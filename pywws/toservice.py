#!/usr/bin/env python

# pywws - Python software for USB Wireless Weather Stations
# http://github.com/jim-easterbrook/pywws
# Copyright (C) 2008-14  Jim Easterbrook  jim@jim-easterbrook.me.uk

# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.

"""Post weather update to services such as Weather Underground
::

%s

Introduction
------------

There are an increasing number of web sites around the world that
encourage amateur weather station owners to upload data over the
internet.

This module enables pywws to upload readings to these organisations.
It is highly customisable using configuration files. Each 'service'
requires a configuration file and one or two templates in
``pywws/services`` (that should not need to be edited by the user) and
a section in ``weather.ini`` containing user specific data such as
your site ID and password.

See :ref:`How to integrate pywws with various weather services
<guides-integration-other>` for details of the available services.

Configuration
-------------

If you haven't already done so, visit the organisation's web site and
create an account for your weather station. Make a note of any site ID
and password details you are given.

Stop any pywws software that is running and then run ``toservice`` to
create a section in ``weather.ini``::

    python -m pywws.toservice data_dir service_name

``service_name`` is the single word service name used by pywws, such
as ``metoffice``, ``data_dir`` is your weather data directory, as
usual.

Edit ``weather.ini`` and find the section corresponding to the service
name, e.g. ``[underground]``. Copy your site details into this
section, for example::

    [underground]
    password = secret
    station = ABCDEFG1A

Now you can test your configuration::

    python -m pywws.toservice -vvv data_dir service_name

This should show you the data string that is uploaded. Any failure
should generate an error message.

Upload old data
---------------

Now you can upload your last 7 days' data, if the service supports it.
Edit your ``status.ini`` file and remove the appropriate line from the
``last update`` section, then run ``toservice`` with the catchup
option::

    python -m pywws.toservice -cvv data_dir service_name

This may take 20 minutes or more, depending on how much data you have.

Add service(s) upload to regular tasks
--------------------------------------

Edit your ``weather.ini`` again, and add a list of services to the
``[live]``, ``[logged]``, ``[hourly]``, ``[12 hourly]`` or ``[daily]``
section, depending on how often you want to send data. For example::

    [live]
    twitter = []
    plot = []
    text = []
    services = ['underground_rf']

    [logged]
    twitter = []
    plot = []
    text = []
    services = ['metoffice', 'stacjapogodywawpl']

    [hourly]
    twitter = []
    plot = []
    text = []
    services = ['underground']

Note that the ``[live]`` section is only used when running
:py:mod:`~pywws.LiveLog`. It is a good idea to repeat any service
selected in ``[live]`` in the ``[logged]`` or ``[hourly]`` section in
case you switch to running :py:mod:`~pywws.Hourly`.

Restart your regular pywws program (:py:mod:`~pywws.Hourly` or
:py:mod:`~pywws.LiveLog`) and visit the appropriate web site to see
regular updates from your weather station.

API
---

"""

__docformat__ = "restructuredtext en"
__usage__ = """
 usage: python -m pywws.toservice [options] data_dir service_name
 options are:
  -h or --help     display this help
  -c or --catchup  upload all data since last upload
  -v or --verbose  increase amount of reassuring messages
 data_dir is the root directory of the weather data
 service_name is the service to upload to, e.g. underground
"""
__doc__ %= __usage__
__usage__ = __doc__.split('\n')[0] + __usage__

from ConfigParser import SafeConfigParser
import getopt
import logging
import os
import re
import socket
import sys
import urllib
import urllib2
from datetime import datetime, timedelta

from pywws import DataStore
from pywws.Logger import ApplicationLogger
from pywws import Template

FIVE_MINS = timedelta(minutes=5)

class ToService(object):
    """Upload weather data to weather services such as Weather
    Underground.

    """
    def __init__(self, params, status, calib_data, service_name):
        """

        :param params: pywws configuration.

        :type params: :class:`pywws.DataStore.params`
        
        :param status: pywws status store.

        :type status: :class:`pywws.DataStore.status`
        
        :param calib_data: 'calibrated' data.

        :type calib_data: :class:`pywws.DataStore.calib_store`

        :param service_name: name of service to upload to.

        :type service_name: string
    
        """
        self.logger = logging.getLogger('pywws.ToService(%s)' % service_name)
        self.params = params
        self.status = status
        self.data = calib_data
        self.service_name = service_name
        # 'derived' services such as 'underground_rf' share their
        # parent's config and templates
        config_section = self.service_name.split('_')[0]
        if config_section == self.service_name:
            self.parent = None
        else:
            self.parent = config_section
        self.old_response = None
        self.old_ex = None
        # set default socket timeout, so urlopen calls don't hang forever
        socket.setdefaulttimeout(30)
        # open params file
        service_params = SafeConfigParser()
        service_params.optionxform = str
        service_params.readfp(open(os.path.join(
            os.path.dirname(__file__), 'services', '%s.ini' % (self.service_name))))
        # get URL
        self.server = service_params.get('config', 'url')
        # get fixed part of upload data
        self.fixed_data = dict()
        for name, value in service_params.items('fixed'):
            if value[0] == '*':
                value = self.params.get(config_section, value[1:], 'unknown')
            self.fixed_data[name] = value
        # create templater
        self.templater = Template.Template(
            self.params, self.status, self.data, self.data, None, None,
            use_locale=False)
        self.template_file = os.path.join(
            os.path.dirname(__file__), 'services', '%s_template_%s.txt' % (
                config_section, self.params.get('config', 'ws type')))
        if not os.path.exists(self.template_file):
            self.template_file = os.path.join(
                os.path.dirname(__file__), 'services',
                '%s_template_1080.txt' % (config_section))
        # get other parameters
        self.catchup = eval(service_params.get('config', 'catchup'))
        self.use_get = eval(service_params.get('config', 'use get'))
        self.expected_result = eval(service_params.get('config', 'result'))
        self.interval = eval(service_params.get('config', 'interval'))
        self.interval = max(self.interval, 40)
        self.interval = timedelta(seconds=self.interval)
        # move 'last update' from params to status
        last_update = self.params.get_datetime(self.service_name, 'last update')
        if last_update:
            self.params.unset(self.service_name, 'last update')
            self.status.set(
                'last update', self.service_name, last_update.isoformat(' '))
        # set timestamp of first data to upload
        if self.catchup <= 0:
            last_update = datetime.utcnow() - self.interval
        else:
            last_update = datetime.utcnow() - timedelta(days=self.catchup)
        if last_update > self.get_last_update():
            self.set_last_update(last_update)

    def get_last_update(self):
        result = self.status.get_datetime('last update', self.service_name)
        if not result:
            result = datetime.min
        if self.parent:
            last_parent = self.status.get_datetime('last update', self.parent)
            if last_parent:
                result = max(result, last_parent)
        return result

    def prepare_data(self, data):
        """Prepare a weather data record.

        The :obj:`data` parameter contains the data to be encoded. It
        should be a 'calibrated' data record, as stored in
        :class:`pywws.DataStore.calib_store`. The relevant data items
        are extracted and converted to strings using a template, then
        merged with the station's "fixed" data.

        :param data: the weather data record.

        :type data: dict

        :return: dict.

        :rtype: string
        
        """
        # check we have external data
        if data['temp_out'] is None:
            return None
        # convert data
        prepared_data = eval(self.templater.make_text(self.template_file, data))
        prepared_data.update(self.fixed_data)
        return prepared_data

    def send_data(self, timestamp, prepared_data):
        """Upload a weather data record.

        The :obj:`prepared_data` parameter contains the data to be uploaded.
        It should be a dictionary of string keys and string values.

        :param timestamp: the timestamp of the data to upload.

        :type timestamp: datetime

        :param prepared_data: the data to upload.

        :type prepared_data: dict

        :return: success status

        :rtype: bool
        
        """
        coded_data = urllib.urlencode(prepared_data)
        self.logger.debug(coded_data)
        try:
            try:
                if self.use_get:
                    wudata = urllib2.urlopen('%s?%s' % (self.server, coded_data))
                else:
                    wudata = urllib2.urlopen(self.server, coded_data)
            except urllib2.HTTPError, ex:
                if ex.code != 400:
                    raise
                wudata = ex
            response = wudata.readlines()
            wudata.close()
            if len(response) == len(self.expected_result):
                for actual, expected in zip(response, self.expected_result):
                    if not re.match(expected, actual):
                        break
                else:
                    self.old_response = response
                    self.set_last_update(timestamp)
                    return True
            if response != self.old_response:
                for line in response:
                    self.logger.error(line.strip())
            self.old_response = response
        except Exception, ex:
            e = str(ex)
            if e != self.old_ex:
                self.logger.error(e)
                self.old_ex = e
        return False

    def next_data(self, catchup, live_data):
        """Get weather data records to upload.

        This method returns either the most recent weather data
        record, or all records since the last upload, according to
        the value of :obj:`catchup`.

        :param catchup: ``True`` to get all records since last upload,
         or ``False`` to get most recent data only.

        :type catchup: boolean

        :param live_data: a current 'live' data record, or ``None``.

        :type live_data: dict

        :return: yields weather data records.

        :rtype: dict
        
        """
        last_update = self.get_last_update()
        if catchup:
            start = last_update
        else:
            start = self.data.before(datetime.max)
        if live_data:
            stop = live_data['idx'] - self.interval
        else:
            stop = None
        next_update = last_update + self.interval
        for data in self.data[start:stop]:
            if data['idx'] >= next_update:
                last_update = data['idx']
                next_update = last_update + self.interval
                yield data
        if live_data and live_data['idx'] >= next_update:
            yield live_data

    def set_last_update(self, timestamp):
        self.status.set(
            'last update', self.service_name, timestamp.isoformat(' '))
        if self.parent:
            last_update = self.status.get_datetime('last update', self.parent)
            if last_update and last_update >= timestamp - FIVE_MINS:
                self.status.set(
                    'last update', self.parent, timestamp.isoformat(' '))

    def Upload(self, catchup=True, live_data=None):
        """Upload one or more weather data records.

        This method uploads either the most recent weather data
        record, or all records since the last upload (up to 7 days),
        according to the value of :obj:`catchup`.

        It sets the ``last update`` configuration value to the time
        stamp of the most recent record successfully uploaded.

        :param catchup: upload all data since last upload.

        :type catchup: bool

        :return: success status

        :rtype: bool
        
        """
        count = 0
        for data in self.next_data(catchup, live_data):
            prepared_data = self.prepare_data(data)
            if not prepared_data:
                continue
            if not self.send_data(data['idx'], prepared_data):
                return False
            count += 1
        if count > 1:
            self.logger.info('%d records sent', count)
        return True

def main(argv=None):
    if argv is None:
        argv = sys.argv
    try:
        opts, args = getopt.getopt(
            argv[1:], "hcv", ['help', 'catchup', 'verbose'])
    except getopt.error, msg:
        print >>sys.stderr, 'Error: %s\n' % msg
        print >>sys.stderr, __usage__.strip()
        return 1
    # process options
    catchup = False
    verbose = 0
    for o, a in opts:
        if o == '-h' or o == '--help':
            print __usage__.strip()
            return 0
        elif o == '-c' or o == '--catchup':
            catchup = True
        elif o == '-v' or o == '--verbose':
            verbose += 1
    # check arguments
    if len(args) != 2:
        print >>sys.stderr, "Error: 2 arguments required"
        print >>sys.stderr, __usage__.strip()
        return 2
    logger = ApplicationLogger(verbose)
    return ToService(
        DataStore.params(args[0]), DataStore.status(args[0]),
        DataStore.calib_store(args[0]), args[1]).Upload(catchup=catchup)

if __name__ == "__main__":
    sys.exit(main())
