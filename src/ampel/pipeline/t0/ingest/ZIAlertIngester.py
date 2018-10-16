#!/usr/bin/env python
# -*- coding: utf-8 -*-
# File              : ampel/pipeline/t0/ingesters/ZIAlertIngester.py
# License           : BSD-3-Clause
# Author            : vb <vbrinnel@physik.hu-berlin.de>
# Date              : 14.12.2017
# Last Modified Date: 15.08.2018
# Last Modified By  : vb <vbrinnel@physik.hu-berlin.de>

import logging, time
from bson.binary import Binary
from bson import ObjectId
from datetime import datetime
from pymongo.errors import BulkWriteError
from pymongo import MongoClient, UpdateOne

from ampel.core.abstract.AbsAlertIngester import AbsAlertIngester
from ampel.pipeline.t0.ingest.CompoundBluePrintGenerator import CompoundBluePrintGenerator
from ampel.pipeline.t0.ingest.ZIPhotoDictShaper import ZIPhotoDictShaper
from ampel.pipeline.t0.ingest.ZICompoundShaper import ZICompoundShaper
from ampel.pipeline.t0.ingest.T2DocsBluePrint import T2DocsBluePrint
from ampel.pipeline.logging.AmpelLogger import AmpelLogger
from ampel.base.flags.AmpelFlags import AmpelFlags
from ampel.base.flags.PhotoFlags import PhotoFlags
from ampel.core.flags.T2RunStates import T2RunStates
from ampel.core.flags.AlDocType import AlDocType
from ampel.core.flags.FlagUtils import FlagUtils
from ampel.pipeline.common.AmpelUtils import AmpelUtils
from ampel.pipeline.config.AmpelConfig import AmpelConfig
from ampel.pipeline.db.AmpelDB import AmpelDB
from ampel.pipeline.t2.T2Controller import T2Controller

SUPERSEEDED = FlagUtils.get_flag_pos_in_enumflag(PhotoFlags.SUPERSEEDED)
TO_RUN = FlagUtils.get_flag_pos_in_enumflag(T2RunStates.TO_RUN)


class ZIAlertIngester(AbsAlertIngester):
	"""
	Ingester class used by t0.AlertProcessor in 'online' mode.
	This class 'ingests' alerts (if they have passed the alert filter):
	it compares info between alert and DB and creates several documents 
	in the DB that are used in later processing stages (T2, T3)
	"""

	# Static vars
	config_path = 'global.sources.ZTFIPAC'
	std_dbflag = FlagUtils.enumflag_to_dbflag(
		AmpelFlags.INST_ZTF|AmpelFlags.SRC_IPAC
	)


	def __init__(self, channels, logger=None, check_reprocessing=True, alert_history_length=30):
		"""
		:param channels: list of ampel.pipeline.config.Channel instances
		:param logger: None or instance of logging.Logger
		:param bool check_reprocessing: whether the ingester should check if photopoints were reprocessed
		(costs an additional DB request per transient). Default is (and should be) True.
		:param int alert_history_length: IPAC currently provides us with a photometric history of 30 days.
		Although this number is unlikely to change, there is no reason to use a constant in code.
		"""

		if type(channels) not in (list, tuple) or not channels:
			raise ValueError("Parameter channels must be a non-empty sequence")

		self.channel_names = tuple(channel.name for channel in channels)
		self.logger = AmpelLogger.get_logger() if logger is None else logger
		self.logger.info("Configuring ZIAlertIngester for channels %s" % repr(self.channel_names))

		t2_units = set()
		for channel in channels:
			t2_units.update(channel.t2_units)

		# T2 unit making use of upper limits
		self.t2_units_using_uls = tuple(
			getattr(T2Controller.load_unit(t2_unit, self.logger), 'upperLimits', False)
			for t2_unit in t2_units
		)

		# instantiate util classes used in method ingest()
		self.photo_shaper = ZIPhotoDictShaper()
		self.t2_blueprint_creator = T2DocsBluePrint(channels, self.t2_units_using_uls)
		self.comp_bp_generator = CompoundBluePrintGenerator(channels, ZICompoundShaper, self.logger)

		# Refs to photopoints/upperlimits and ampel main DB collections
		self.main_col = AmpelDB.get_collection('main')
		self.photo_col = AmpelDB.get_collection('photo')

		# JD2017 is used to defined upper limits primary IDs
		self.JD2017 = 2457754.5

		# Stats
		self.count_dict = None

		# Standard projection used when checking DB for existing PPS/ULS
		self.lookup_projection = {
			"_id": 1, "alFlags": 1, "jd": 1, "fid": 1,
			"rcid": 1, "alExcluded": 1, "magpsf": 1
		}

		# Global config whether to check for IPAC PPS reprocessing
		self.check_reprocessing = check_reprocessing

		# Global config defining the std IPAC alert history length. As of June 2018: 30 days
		self.alert_history_length = alert_history_length

		# Feedback
		self.logger.info("ZIAlertIngester setup using completed")


	def set_log_id(self, log_id):
		"""
		:param log_id: int
		An ingester class creates/updates several documents in the DB for each alert.
		Among other things, it updates the main transient document, 
		which contains a list of log run ids associated with the processing of the given transient.
		We thus need to know what is the current job_id to perform this update.
		The provided parameter should be a bson ObjectId.
		"""
		#if type(log_id) is not int:
		#	raise ValueError("Illegal argument type: %s" % type(log_id))

		self.job_id = log_id


	def set_stats_dict(self, time_dict, count_dict):
		"""
		"""

		self.count_dict = count_dict
		self.time_dict = time_dict

		for el in ('dbBulkTimePhoto', 'dbBulkTimeMain', 'dbPerOpMeanTimePhoto', 'dbPerOpMeanTimeMain'):
			if not el in time_dict:
				time_dict[el] = []

		for key in ('pps', 'uls', 't2s', 'comps', 'ppReprocs'):
			self.count_dict[key] = 0
			

	def set_photodict_shaper(self, arg_photo_shaper):
		"""
		Before the ingester instance inserts new photopoints or upper limits into the database, 
		it 'customizes' (or 'ampelizes' if you will) them in order to later enable
		the use of short and flexible queries. 
		The cutomizations are minimal, most of the original structure is kept.
		For exmample, in the case of ZIPhotoDictShaper:
			* The field candid is renamed in _id 
			* A new field 'alFlags' (AmpelFlags) is created (integer value of ampel.base.flags.PhotoFlags)
			* A new field 'alDocType' is created (integer value of ampel.core.flags.AlDocType.PHOTOPOINT/UPPERLIMIT)
		A photopoint shaper class (t0.pipeline.ingesters...) performs these operations.
		This method enables the customization of the PhotoDictShaper instance to be used.
		By default, ZIPhotoDictStamper is used.
		"""
		self.photo_shaper = arg_photo_shaper


	def get_photodict_shaper(self):
		"""
		Get the PhotoDictShaper instance associated with this class instance.
		For more information, please check the set_photodict_shaper docstring
		"""
		return self.photo_shaper


	def ingest(self, tran_id, pps_alert, uls_alert, list_of_t2_units):
		"""
		This method is called by t0.AmpelProcessor for alerts passing at least one T0 channel filter. 
		Photopoints, transients and  t2 documents are created and saved into the DB.
		Note: Some dict instances referenced in pps_alert and uls_alert might be modified by this method.
		"""

		###############################################
		##   Part 1: Gather info from DB and alert   ##
		###############################################

		# Save alert id
		alert_id = pps_alert[0]['candid']

		# pymongo bulk op array
		db_photo_ops = []
		db_main_ops = []

		# metrics
		pps_reprocs = 0
		t2_upserts = 0
		compound_upserts = 0
		start = time.time()

		# Load existing photopoint and upper limits from DB if any
		self.logger.info("Checking DB for existing pps/uls")
		meas_db = self.photo_col.find(
			{
				# tranId should be specific to one instrument
				"tranId": tran_id
			},
			self.lookup_projection
		)

		pps_db = []
		uls_db = []

		# Create pps / uls lists from (mixed) db results
		for el in meas_db:
			if 'magpsf' in el:  
				pps_db.append(el) # Photopoint
			else:
				uls_db.append(el) # Upper limit

		# Default refs to empty list (list concatenation occurs later)
		pps_to_insert = []
		uls_to_insert = []

		# Create set with pp ids from alert
		ids_pps_alert = {pp['candid'] for pp in pps_alert}

		# python set of ids of photopoints from DB
		ids_pps_db = {el['_id'] for el in pps_db}

		# Set of uls ids from alert
		ids_uls_alert = set()

		# Create unique ids for the upper limits from alert
		# Concrete example:
		# {
		#   'diffmaglim': 19.024799346923828,
 		#   'fid': 2,
 		#   'jd': 2458089.7405324,
 		#   'pdiffimfilename': '/ztf/archive/sci/2017/1202/240532/ \
		#                      ztf_20171202240532_000566_zr_c08_o_q1_scimrefdiffimg.fits.fz',
 		#   'pid': 335240532815,
 		#   'programid': 0
		# }
		# -> generated ID: -3352405322819025
		if uls_alert is not None:

			for ul in uls_alert:

				# extract quadrant number from pid (not avail as dedicate key/val)
				rcid = str(ul['pid'])[8:10]
				ul['rcid'] = int(rcid)

				# Update avro dict
				ul['_id'] = int(
					"%i%s%i" % (
						# Convert jd float into int by multiplying it by 10**6, we thereby 
						# drop the last digit (milisecond) which is pointless for our purpose
						(self.JD2017 - ul['jd']) * 1000000, 
						# cut of mag float after 3 digits after coma
						rcid, round(ul['diffmaglim'] * 1000)
					)
				)

				ids_uls_alert.add(ul['_id'])

		# python set of ids of upper limits from DB
		ids_uls_db = {el['_id'] for el in uls_db}

		# If no photopoint exists in the DB, then this is a new transient 
		if not ids_pps_db:
			self.logger.info("Transient is new")



		#################################################################
		##   Part 2: Insert new photopoints and upper limits into DB   ##
		#################################################################

		# Difference between candids from the alert and candids present in DB 
		ids_pps_to_insert = ids_pps_alert - ids_pps_db
		ids_uls_to_insert = ids_uls_alert - ids_uls_db

		# If the photopoints already exist in DB 

		# PHOTO POINTS
		if ids_pps_to_insert:

			self.logger.info(
				"%i new photo point(s) will be upserted: %s" % 
				(len(ids_pps_to_insert), ids_pps_to_insert)
			)

			# ForEach photopoint not existing in DB: 
			# Rename candid into _id, add tranId, alDocType (PHOTOPOINT) and alFlags
			# Attention: ampelize *modifies* dict instances loaded by fastavro
			pps_to_insert = self.photo_shaper.ampelize(
				tran_id, pps_alert, ids_pps_to_insert
			)

			for pp in pps_to_insert:
				db_photo_ops.append(
					UpdateOne(
						{"_id": pp["_id"]},
						{"$setOnInsert": pp},
						upsert=True
					)
				)
		else:
			self.logger.info("No photopoint db update required")

		# UPPER LIMITS
		if ids_uls_to_insert:

			self.logger.info(
				"%i upper limit(s) will be inserted/updated: %s" % 
				(len(ids_uls_to_insert), ids_uls_to_insert)
			)

			# For each upper limit not existing in DB: 
			# Add tranId, alDocType (UPPER_LIMIT) and alFlags
			# Attention: ampelize *modifies* dict instances loaded by fastavro
			uls_to_insert = self.photo_shaper.ampelize(
				tran_id, uls_alert, ids_uls_to_insert, id_field_name='_id'
			)

			# Insert new upper limit into DB
			for ul in uls_to_insert:
				db_photo_ops.append(
					UpdateOne(
						{"_id": ul["_id"]},
						{
							"$setOnInsert": ul,
							"$addToSet": {'tranId': tran_id}
						},
						upsert=True
					)
				)
		else:
			self.logger.info("No upper limit db update required")




		###################################################
		##   Part 3: Check for reprocessed photopoints   ##
		###################################################

		# NOTE: this procedure will *update* selected the dict instances 
		# loaded from DB (from the lists: pps_db and uls_db)

		# Difference between candids from db and candids from alert
		ids_in_db_not_in_alert = (ids_pps_db | ids_uls_db) - (ids_pps_alert | ids_uls_alert)

		# If the set is not empty, either some transient info is older that alert_history_length days
		# or some photopoints were reprocessed
		if self.check_reprocessing and ids_in_db_not_in_alert:

			# Ignore ppts in db older than alert_history_length days  
			min_jd = pps_alert[0]["jd"] - self.alert_history_length
			ids_in_db_older_than_xx_days = {el["_id"] for el in pps_db + uls_db if el["jd"] < min_jd}
			ids_superseeded = ids_in_db_not_in_alert - ids_in_db_older_than_xx_days

			# pps/uls reprocessing occured at IPAC
			if ids_superseeded:

				# loop through superseeded photopoint
				for photod_db_superseeded in filter(
					lambda x: x['_id'] in ids_superseeded, pps_db + uls_db
				):

					# Match these with new alert data (already 'shaped' by the ampelize method)
					for new_meas in filter(lambda x: 
						# jd alone is actually enough for matching pps reproc 
						x['jd'] == photod_db_superseeded['jd'] and 
						x['rcid'] == photod_db_superseeded['rcid'], 
						pps_to_insert + uls_to_insert
					):

						self.logger.info(
							"Marking measurement %s as superseeded by %s",
							photod_db_superseeded["_id"], 
							new_meas['_id']
						)

						# Update flags in dict loaded by fastavro
						# (required for t2 & compounds doc creation)
						if SUPERSEEDED not in photod_db_superseeded['alFlags']:
							photod_db_superseeded['alFlags'].append(SUPERSEEDED)

						# Create and append pymongo update operation
						pps_reprocs += 1
						db_photo_ops.append(
							UpdateOne(
								{'_id': photod_db_superseeded["_id"]}, 
								{
									'$addToSet': {
										'newId': new_meas['_id'],
										'alFlags': SUPERSEEDED
									}
								}
							)
						)
			else:
				self.logger.info("Transient data older than 30 days exist in DB")




		#####################################################
		##   Part 4: Generate compound ids and compounds   ##
		#####################################################

		# Generate tuple of channel names
		chan_names = tuple(
			chan_name for chan_name, t2_units in zip(self.channel_names, list_of_t2_units) 
			if t2_units is not None
		)

		# Compute compound ids (used later for creating compounds and t2 docs)
		comp_bp = self.comp_bp_generator.generate(
			tran_id,
			sorted(
				pps_db + pps_to_insert + uls_db + uls_to_insert, 
				key=lambda k: k['jd']
			),
			# Do computation only for chans having passed T0 filters (not None)
			chan_names
		)

		# See how many different eff_comp_id were generated (possibly a single one)
		# and generate corresponding ampel document to be inserted later
		for eff_comp_id in comp_bp.get_effids_of_chans(chan_names):
		
			d_addtoset = {
				"channels": {
					"$each": list(
						comp_bp.get_chans_with_effid(eff_comp_id)
					)
				}
			}

			if comp_bp.has_flavors(eff_comp_id):
				d_addtoset["flavors"] = {
					# returns tuple
					"$each": comp_bp.get_compound_flavors(eff_comp_id)
				}
			
			comp_dict = comp_bp.get_eff_compound(eff_comp_id)
			pp_comp_id = comp_bp.get_ppid_of_effid(eff_comp_id)
			bson_eff_comp_id = Binary(eff_comp_id, 5)

			d_set_on_insert =  {
				"_id": bson_eff_comp_id,
				"tranId": tran_id,
				"alDocType": AlDocType.COMPOUND,
				"alFlags": FlagUtils.enumflag_to_dbflag(
					comp_bp.get_comp_flags(eff_comp_id)
				),
				"tier": 0,
				"added": datetime.utcnow().timestamp(),
				"lastJD": pps_alert[0]['jd'],
				"len": len(comp_dict),
				"comp": comp_dict
			}

			if pp_comp_id != eff_comp_id:
				d_set_on_insert['ppId'] = Binary(pp_comp_id, 5)

			compound_upserts += 1

			db_main_ops.append(
				UpdateOne(
					{"_id": bson_eff_comp_id},
					{
						"$setOnInsert": d_set_on_insert,
						"$addToSet": d_addtoset
					},
					upsert=True
				)
			)

			


		#####################################
		##   Part 5: Generate t2 documents ##
		#####################################

		self.logger.info("Generating T2 docs")
		t2docs_blueprint = self.t2_blueprint_creator.create_blueprint(
			comp_bp, list_of_t2_units
		)
		
		# counter for user feedback (after next loop)
		now = int(datetime.utcnow().timestamp())

		# Loop over t2 runnables
		for t2_id in t2docs_blueprint.keys():

			# Loop over run settings
			for run_config in t2docs_blueprint[t2_id].keys():
			
				# Loop over compound Ids
				for bifold_comp_id in t2docs_blueprint[t2_id][run_config]:

					# Set of channel names
					eff_chan_names = list( # pymongo requires list
						t2docs_blueprint[t2_id][run_config][bifold_comp_id]
					)

					bson_bifold_comp_id = Binary(bifold_comp_id, 5)

					journal = []

					# Matching search criteria
					match_dict = {
						"tranId": tran_id,
						"alDocType": AlDocType.T2RECORD,
						"t2Unit": t2_id, 
						"runConfig": run_config
					}

					# Attributes set if no previous doc exists
					d_set_on_insert = {
						"tranId": tran_id,
						"alDocType": AlDocType.T2RECORD,
						"alFlags": ZIAlertIngester.std_dbflag,
						"t2Unit": t2_id, 
						"runConfig": run_config, 
						"runState": TO_RUN
					}

					# Update set of channels
					d_addtoset = {
						"channels": {
							"$each": eff_chan_names
						}
					}

					# T2 doc referencing multiple compound ids (== T2 ignoring upper limits)
					# bifold_comp_id is then a pp_compound_id
					if t2_id not in self.t2_units_using_uls:

						# match_dict["compId"] = bifold_comp_id or 
						# match_dict["compId"] = {"$in": [bifold_comp_id]}
						# triggers the error: "Cannot apply $addToSet to non-array field. \
						# Field named 'compId' has non-array type string"
						# -> See https://jira.mongodb.org/browse/SERVER-3946
						match_dict["compId"] = {
							"$elemMatch": {
								"$eq": bson_bifold_comp_id
							}
						}

						d_addtoset["compId"] = {
							"$each": [
								Binary(el, 5) for el in (
									{bifold_comp_id} | 
									comp_bp.get_effids_of_chans(eff_chan_names)
								)
							]
						}

						# Update journal: register eff id for each channel
						journal_entries = [
							{
								"tier": 0,
								"dt": now,
								"channel(s)": chan_name,
								"effId": comp_bp.get_effid_of_chan(chan_name)
							}
							for chan_name in eff_chan_names
						]

						# Update journal: register pp id common to all channels
						journal_entries.insert(0, 
							{
								"tier": 0,
								"dt": now,
								"channel(s)": AmpelUtils.try_reduce(eff_chan_names),
								"ppId": bson_bifold_comp_id
							}
						)

						# Update journal
						d_addtoset["journal"] = {"$each": journal_entries}

					# T2 doc referencing a single compound id
					# bifold_comp_id is then an eff_compound_id
					else:

						match_dict["compId"] = bson_bifold_comp_id

						# list is required for later $addToSet operations to succeed
						d_set_on_insert["compId"] = [bson_bifold_comp_id]

						# Update journal
						d_addtoset["journal"] = {
							"tier": 0,
							"dt": now,
							"channel(s)": AmpelUtils.try_reduce(eff_chan_names)
						}

					t2_upserts += 1

					# Append update operation to bulk list
					db_main_ops.append(
						UpdateOne(
							match_dict,
							{
								"$setOnInsert": d_set_on_insert,
								"$addToSet": d_addtoset
							},
							upsert=True
						)
					)


		# Insert generated t2 docs into collection
		self.logger.info("%i T2 docs will be inserted into DB", t2_upserts)



		############################################
		##   Part 6: Update transient documents   ##
		############################################

		# Insert/Update transient document into 'transients' collection
		self.logger.info("Updating transient document")

		# TODO add alFlags
		db_main_ops.append(
			UpdateOne(
				{
					"tranId": tran_id,
					"alDocType": AlDocType.TRANSIENT
				},
				{
					"$setOnInsert": {
						"tranId": tran_id,
						"alDocType": AlDocType.TRANSIENT,
					},
					'$addToSet': {
						"alFlags": {
							"$each": ZIAlertIngester.std_dbflag
						},
						'channels': (
							chan_names[0] if len(chan_names) == 1 
							else {"$each": chan_names}
						)
					},
					"$max": {
						"lastPPJD": pps_alert[0]["jd"],
						"modified": now
					},
					"$push": {
						"journal": {
							'tier': 0,
							'dt': now,
							'channel(s)': AmpelUtils.try_reduce(chan_names),
							'alertId': alert_id,
							'logs': self.job_id
						}
					}
				},
				upsert=True
			)
		)

		# Save time required by python for this method so far
		self.time_dict['preIngestTime'].append(time.time() - start)

		# Perform 'photo' DB operations
		if db_photo_ops:
			self.update_db(self.photo_col, db_photo_ops)

		# Perform 'main' DB operations
		if db_main_ops:
			self.update_db(self.main_col, db_main_ops)

		# Update counter metrics
		if self.count_dict is not None:
			self.count_dict['pps'] += len(pps_to_insert)
			self.count_dict['uls'] += len(uls_to_insert)
			self.count_dict['t2s'] += t2_upserts
			self.count_dict['comps'] += compound_upserts
			self.count_dict['ppReprocs'] += pps_reprocs


	def update_db(self, col, ops):
		"""
		Regarding the handling of BulkWriteError:
		Concurent upserts triggers a DuplicateKeyError exception.

		https://stackoverflow.com/questions/37295648/mongoose-duplicate-key-error-with-upsert
		<quote>
			An upsert that results in a document insert is not a fully atomic operation. 
			Think of the upsert as performing the following discrete steps:
    			Query for the identified document to upsert.
    			If the document exists, atomically update the existing document.
    			Else (the document doesn't exist), atomically insert a new document 
				that incorporates the query fields and the update.
		</quote>

		There are *many* tickets opened on the mongoDB bug tracker regarding this issue.
		One of which: https://jira.mongodb.org/browse/SERVER-14322
		where is stated:
			"It is expected that the client will take appropriate action 
			upon detection of such constraint violation"

		All in all: the server behaves inappropriately, the driver won't catch those 
		cases for us, so we have to do the work by ourself. Great.

		Last: the use of SON (serialized Ocument Normalisation) is deprecated according 
		to the mongoDB doc. It will be removed with pymongo 4, so we should not use it anymore.
		BUT: the offending updates (UpdateOne instances) returned by the server are 
		provided as SON by BulkWriteError (array 'writeErrors' contains SON objects).
		So we have no other choice than handling with them for now.
		"""

		try: 

			# DB insertion time is measured
			if self.count_dict is not None:

				# Update DB
				start = time.time()
				db_res = col.bulk_write(ops, ordered=False)
				time_delta = time.time() - start

				# Save metrics
				self.time_dict['dbBulkTime%s' % col.name.title()].append(time_delta)
				self.time_dict['dbPerOpMeanTime%s' % col.name.title()].append(time_delta / len(ops))

			# no metric
			else:
				db_res = col.bulk_write(ops, ordered=False)

			self.logger.info(
				"DB %s feeback: %i upserted, %i modified" % (
					col.name,
					db_res.bulk_api_result['nUpserted'],
					db_res.bulk_api_result['nModified']
				)
			)

		# Catch BulkWriteError only, other exceptions are caught in AlertProcessor
		except BulkWriteError as bwe: 

			for err_dict in bwe.details.get('writeErrors', []):

				# 'code': 11000, 'errmsg': 'E11000 duplicate key error collection: ...
				if err_dict.get("code") == 11000:

					self.logger.info(
						"Race condition during insertion in '%s': %s" % (
							col.name, err_dict
						)
					)

					# Should not throw pymongo.errors.DuplicateKeyError
					col.update_one(
						err_dict['op']['q'], 
						err_dict['op']['u'], 
						upsert=err_dict['op']['upsert']
					)

					self.logger.info("Error recovered")

					# DB insertion time is measured
					if self.count_dict is not None:
						time_delta = time.time() - start
						self.time_dict['dbBulkTime%s' % col.name.title()].append(time_delta)
						self.time_dict['dbPerOpMeanTime%s' % col.name.title()].append(
							time_delta / len(ops)
						)

				else:
					self.logger.error(bwe.details) 
					raise bwe

			self.logger.info(
				"DB %s feeback: %i upserted, %i modified, %i race condition(s) recovered" % (
					col.name,
					bwe.details['nUpserted'],
					bwe.details['nModified'],
					len(bwe.details.get('writeErrors'))
				)
			)