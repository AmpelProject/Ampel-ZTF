#!/usr/bin/env python
# -*- coding: utf-8 -*-
# File              : ampel/pipeline/t0/ingesters/ZIPhotoDictShaper.py
# License           : BSD-3-Clause
# Author            : vb <vbrinnel@physik.hu-berlin.de>
# Date              : 14.12.2017
# Last Modified Date: 14.07.2018
# Last Modified By  : vb <vbrinnel@physik.hu-berlin.de>

from ampel.base.flags.PhotoFlags import PhotoFlags
from ampel.core.flags.FlagUtils import FlagUtils
from ampel.core.flags.AlDocType import AlDocType
import bson

class ZIPhotoDictShaper:
	"""
	Shapes photometric points and upper limits into dicts that ampel understands.
	-> add tranId and alFlags
	-> for photometric points: rename candid into _id
	-> for upper limits: _id was computed previously
	"""

	# Public / private data
	# 0: 'All'
	# 1: 'Public'
	# 2: 'ZtfCollaboration'
	# 3: 'Caltech'
	d_programid = {
		0: PhotoFlags.ZTF_PUBLIC | PhotoFlags.ZTF_COLLAB,	
		1: PhotoFlags.ZTF_PUBLIC,	
		2: PhotoFlags.ZTF_COLLAB,
		3: PhotoFlags.ZTF_COLLAB # actually CALTEC
	}

	# Filter color
	d_filterid = {
		1: PhotoFlags.BAND_ZTF_G,
		2: PhotoFlags.BAND_ZTF_R,
		3: PhotoFlags.BAND_ZTF_I
	}


	def __init__(self):
		"""
		"""
		# pylint: disable=no-member
		self.base_flags = PhotoFlags.INST_ZTF|PhotoFlags.SRC_IPAC


	def append_base_flags(self, flags):
		"""
		"""
		self.base_flags |= flags


	def ampelize(self, tran_id, photo_list, ids_set, id_field_name='candid'):
		"""
		tran_id: transient id (string)
		photo_list: list of dict instance (respresenting photopoint/upper limit measurements).
		ids_set: set of strings containing photopoint/upperlimit ids. 
		Only the elements from photo_list matching the provided ids will be processed.
		"""

		ret_list = []

		# Micro optimization
		d_programid = ZIPhotoDictShaper.d_programid
		d_filterid = ZIPhotoDictShaper.d_filterid
		to_dbflag = FlagUtils.enumflag_to_dbflag

		for photo_dict in photo_list:

			if photo_dict[id_field_name] not in ids_set:
				continue

			# Base flags
			ppflags = self.base_flags
	
			# Public / private data
			ppflags |= d_programid[photo_dict['programid']]
	
			# Filter color
			ppflags |= d_filterid[photo_dict['fid']]

			# Compute ampel flags
			dbflags = to_dbflag(ppflags)

			# photopoints come with 'candid'. 
			# '_id' is already set for upper limits
			if id_field_name != "_id":

				# Set ampel flags and tranId
				photo_dict['alFlags'] = dbflags
				photo_dict['tranId'] = tran_id
				
				# Cut path if present
				if photo_dict.get('pdiffimfilename') is not None:
					photo_dict['pdiffimfilename'] = photo_dict['pdiffimfilename'].split('/')[-1].replace('.fz', '')

				# Rename 'candid' into '_id'
				photo_dict['_id'] = photo_dict[id_field_name]
				del photo_dict[id_field_name]

				# update list of dicts
				ret_list.append(photo_dict)

			else:

				# update list of dicts
				ret_list.append(
					{
						'_id': photo_dict['_id'],
						# 'alDocType': AlDocTypes.UPPERLIMIT,
						'alFlags': dbflags,
						'jd': photo_dict['jd'],
						'diffmaglim': photo_dict['diffmaglim'],
						'rcid': photo_dict['rcid'],
						'fid': photo_dict['fid']
						#'pdiffimfilename': fname
						# IMPORTANT: 'tranId' is not set here on purpose since it 
						# then conflicts with the addToSet operation
						#'pid': photo_dict['pid'],
						#'programid': photo_dict['programid'],
					}
				)

		# Return created list
		return ret_list