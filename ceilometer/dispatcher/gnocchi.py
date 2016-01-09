#
# Copyright 2014-2015 eNovance
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
from collections import defaultdict
from hashlib import md5
import itertools
import operator
import threading
import uuid

from gnocchiclient import client
from gnocchiclient import exceptions as gnocchi_exc
from keystoneauth1 import session as ka_session
from oslo_config import cfg
from oslo_log import log
import requests
import retrying
import six
from stevedore import extension

from ceilometer import declarative
from ceilometer import dispatcher
from ceilometer.i18n import _, _LE, _LW
from ceilometer import keystone_client
from ceilometer import utils

NAME_ENCODED = __name__.encode('utf-8')
CACHE_NAMESPACE = uuid.UUID(bytes=md5(NAME_ENCODED).digest())
LOG = log.getLogger(__name__)

dispatcher_opts = [
    cfg.BoolOpt('filter_service_activity',
                default=True,
                help='Filter out samples generated by Gnocchi '
                'service activity'),
    cfg.StrOpt('filter_project',
               default='gnocchi',
               help='Gnocchi project used to filter out samples '
               'generated by Gnocchi service activity'),
    cfg.StrOpt('url',
               deprecated_for_removal=True,
               help='URL to Gnocchi. default: autodetection'),
    cfg.StrOpt('archive_policy',
               default=None,
               help='The archive policy to use when the dispatcher '
               'create a new metric.'),
    cfg.StrOpt('resources_definition_file',
               default='gnocchi_resources.yaml',
               help=_('The Yaml file that defines mapping between samples '
                      'and gnocchi resources/metrics')),
]

cfg.CONF.register_opts(dispatcher_opts, group="dispatcher_gnocchi")


def cache_key_mangler(key):
    """Construct an opaque cache key."""
    if six.PY2:
        key = key.encode('utf-8')
    return uuid.uuid5(CACHE_NAMESPACE, key).hex


def log_and_ignore_unexpected_workflow_error(func):
    def log_and_ignore(self, *args, **kwargs):
        try:
            func(self, *args, **kwargs)
        except gnocchi_exc.ClientException as e:
            LOG.error(six.text_type(e))
        except Exception as e:
            LOG.error(six.text_type(e), exc_info=True)
    return log_and_ignore


class ResourcesDefinitionException(Exception):
    def __init__(self, message, definition_cfg):
        super(ResourcesDefinitionException, self).__init__(message)
        self.definition_cfg = definition_cfg

    def __str__(self):
        return '%s %s: %s' % (self.__class__.__name__,
                              self.definition_cfg, self.message)


class ResourcesDefinition(object):

    MANDATORY_FIELDS = {'resource_type': six.string_types,
                        'metrics': list}

    def __init__(self, definition_cfg, default_archive_policy, plugin_manager):
        self._default_archive_policy = default_archive_policy
        self.cfg = definition_cfg

        for field, field_type in self.MANDATORY_FIELDS.items():
            if field not in self.cfg:
                raise declarative.DefinitionException(
                    _LE("Required field %s not specified") % field, self.cfg)
            if not isinstance(self.cfg[field], field_type):
                raise declarative.DefinitionException(
                    _LE("Required field %(field)s should be a %(type)s") %
                    {'field': field, 'type': field_type}, self.cfg)

        self._attributes = {}
        for name, attr_cfg in self.cfg.get('attributes', {}).items():
            self._attributes[name] = declarative.Definition(name, attr_cfg,
                                                            plugin_manager)

        self.metrics = {}
        for t in self.cfg['metrics']:
            archive_policy = self.cfg.get('archive_policy',
                                          self._default_archive_policy)
            if archive_policy is None:
                self.metrics[t] = {}
            else:
                self.metrics[t] = dict(archive_policy_name=archive_policy)

    def match(self, metric_name):
        for t in self.cfg['metrics']:
            if utils.match(metric_name, t):
                return True
        return False

    def attributes(self, sample):
        attrs = {}
        for name, definition in self._attributes.items():
            value = definition.parse(sample)
            if value is not None:
                attrs[name] = value
        return attrs


def get_gnocchiclient(conf):
    requests_session = requests.session()
    for scheme in requests_session.adapters.keys():
        requests_session.mount(scheme, ka_session.TCPKeepAliveAdapter(
            pool_block=True))

    session = keystone_client.get_session(requests_session=requests_session)
    return client.Client('1', session,
                         interface=conf.service_credentials.interface,
                         region_name=conf.service_credentials.region_name,
                         endpoint_override=conf.dispatcher_gnocchi.url)


class LockedDefaultDict(defaultdict):
    """defaultdict with lock to handle threading

    Dictionary only deletes if nothing is accessing dict and nothing is holding
    lock to be deleted. If both cases are not true, it will skip delete.
    """
    def __init__(self, *args, **kwargs):
        self.lock = threading.Lock()
        super(LockedDefaultDict, self).__init__(*args, **kwargs)

    def __getitem__(self, key):
        with self.lock:
            return super(LockedDefaultDict, self).__getitem__(key)

    def __delitem__(self, key):
        with self.lock:
            with self.__getitem__(key)(blocking=False):
                super(LockedDefaultDict, self).__delitem__(key)


class GnocchiDispatcher(dispatcher.MeterDispatcherBase):
    """Dispatcher class for recording metering data into database.

    The dispatcher class records each meter into the gnocchi service
    configured in ceilometer configuration file. An example configuration may
    look like the following:

    [dispatcher_gnocchi]
    url = http://localhost:8041
    archive_policy = low

    To enable this dispatcher, the following section needs to be present in
    ceilometer.conf file

    [DEFAULT]
    meter_dispatchers = gnocchi
    """
    def __init__(self, conf):
        super(GnocchiDispatcher, self).__init__(conf)
        self.conf = conf
        self.filter_service_activity = (
            conf.dispatcher_gnocchi.filter_service_activity)
        self._ks_client = keystone_client.get_client()
        self.resources_definition = self._load_resources_definitions(conf)

        self.cache = None
        try:
            import oslo_cache
            oslo_cache.configure(self.conf)
            # NOTE(cdent): The default cache backend is a real but
            # noop backend. We don't want to use that here because
            # we want to avoid the cache pathways entirely if the
            # cache has not been configured explicitly.
            if 'null' not in self.conf.cache.backend:
                cache_region = oslo_cache.create_region()
                self.cache = oslo_cache.configure_cache_region(
                    self.conf, cache_region)
                self.cache.key_mangler = cache_key_mangler
        except ImportError:
            pass
        except oslo_cache.exception.ConfigurationError as exc:
            LOG.warning(_LW('unable to configure oslo_cache: %s') % exc)

        self._gnocchi_project_id = None
        self._gnocchi_project_id_lock = threading.Lock()
        self._gnocchi_resource_lock = LockedDefaultDict(threading.Lock)

        self._gnocchi = get_gnocchiclient(conf)
        # Convert retry_interval secs to msecs for retry decorator
        retries = conf.storage.max_retries

        @retrying.retry(wait_fixed=conf.storage.retry_interval * 1000,
                        stop_max_attempt_number=(retries if retries >= 0
                                                 else None))
        def _get_connection():
            self._gnocchi.capabilities.list()

        try:
            _get_connection()
        except Exception:
            LOG.error(_LE('Failed to connect to Gnocchi.'))
            raise

    @classmethod
    def _load_resources_definitions(cls, conf):
        plugin_manager = extension.ExtensionManager(
            namespace='ceilometer.event.trait_plugin')
        data = declarative.load_definitions(
            {}, conf.dispatcher_gnocchi.resources_definition_file)
        return [ResourcesDefinition(r, conf.dispatcher_gnocchi.archive_policy,
                                    plugin_manager)
                for r in data.get('resources', [])]

    @property
    def gnocchi_project_id(self):
        if self._gnocchi_project_id is not None:
            return self._gnocchi_project_id
        with self._gnocchi_project_id_lock:
            if self._gnocchi_project_id is None:
                try:
                    project = self._ks_client.projects.find(
                        name=self.conf.dispatcher_gnocchi.filter_project)
                except Exception:
                    LOG.exception('fail to retrieve user of Gnocchi service')
                    raise
                self._gnocchi_project_id = project.id
                LOG.debug("gnocchi project found: %s", self.gnocchi_project_id)
            return self._gnocchi_project_id

    def _is_swift_account_sample(self, sample):
        return bool([rd for rd in self.resources_definition
                     if rd.cfg['resource_type'] == 'swift_account'
                     and rd.match(sample['counter_name'])])

    def _is_gnocchi_activity(self, sample):
        return (self.filter_service_activity and (
            # avoid anything from the user used by gnocchi
            sample['project_id'] == self.gnocchi_project_id or
            # avoid anything in the swift account used by gnocchi
            (sample['resource_id'] == self.gnocchi_project_id and
             self._is_swift_account_sample(sample))
        ))

    def _get_resource_definition(self, metric_name):
        for rd in self.resources_definition:
            if rd.match(metric_name):
                return rd

    def record_metering_data(self, data):
        # We may have receive only one counter on the wire
        if not isinstance(data, list):
            data = [data]
        # NOTE(sileht): skip sample generated by gnocchi itself
        data = [s for s in data if not self._is_gnocchi_activity(s)]

        # FIXME(sileht): This method bulk the processing of samples
        # grouped by resource_id and metric_name but this is not
        # efficient yet because the data received here doesn't often
        # contains a lot of different kind of samples
        # So perhaps the next step will be to pool the received data from
        # message bus.
        data.sort(key=lambda s: (s['resource_id'], s['counter_name']))

        resource_grouped_samples = itertools.groupby(
            data, key=operator.itemgetter('resource_id'))

        for resource_id, samples_of_resource in resource_grouped_samples:
            metric_grouped_samples = itertools.groupby(
                list(samples_of_resource),
                key=operator.itemgetter('counter_name'))

            self._process_resource(resource_id, metric_grouped_samples)

    @log_and_ignore_unexpected_workflow_error
    def _process_resource(self, resource_id, metric_grouped_samples):
        resource_extra = {}
        for metric_name, samples in metric_grouped_samples:
            samples = list(samples)
            rd = self._get_resource_definition(metric_name)
            if rd is None:
                LOG.warning("metric %s is not handled by gnocchi" %
                            metric_name)
                continue
            if rd.cfg.get("ignore"):
                continue

            resource_type = rd.cfg['resource_type']
            resource = {
                "id": resource_id,
                "user_id": samples[0]['user_id'],
                "project_id": samples[0]['project_id'],
                "metrics": rd.metrics,
            }
            measures = []

            for sample in samples:
                resource_extra.update(rd.attributes(sample))
                measures.append({'timestamp': sample['timestamp'],
                                 'value': sample['counter_volume']})

            resource.update(resource_extra)

            retry = True
            try:
                self._gnocchi.metric.add_measures(metric_name, measures,
                                                  resource_id)
            except gnocchi_exc.ResourceNotFound:
                self._if_not_cached("create", resource_type, resource,
                                    self._create_resource)

            except gnocchi_exc.MetricNotFound:
                metric = {'resource_id': resource['id'],
                          'name': metric_name}
                metric.update(rd.metrics[metric_name])
                try:
                    self._gnocchi.metric.create(metric)
                except gnocchi_exc.NamedMetricAreadyExists:
                    # NOTE(sileht): metric created in the meantime
                    pass
            else:
                retry = False

            if retry:
                self._gnocchi.metric.add_measures(metric_name, measures,
                                                  resource_id)
                LOG.debug("Measure posted on metric %s of resource %s",
                          metric_name, resource_id)

        if resource_extra:
            self._if_not_cached("update", resource_type, resource,
                                self._update_resource, resource_extra)

    def _create_resource(self, resource_type, resource):
        try:
            self._gnocchi.resource.create(resource_type, resource)
            LOG.debug('Resource %s created', resource["id"])
        except gnocchi_exc.ResourceAlreadyExists:
            # NOTE(sileht): resource created in the meantime
            pass

    def _update_resource(self, resource_type, resource, resource_extra):
        self._gnocchi.resource.update(resource_type,
                                      resource["id"],
                                      resource_extra)
        LOG.debug('Resource %s updated', resource["id"])

    def _if_not_cached(self, operation, resource_type, resource, method,
                       *args, **kwargs):
        if self.cache:
            cache_key = resource['id']
            attribute_hash = self._check_resource_cache(cache_key, resource)
            if attribute_hash:
                with self._gnocchi_resource_lock[cache_key]:
                    # NOTE(luogangyi): there is a possibility that the
                    # resource was already built in cache by another
                    # ceilometer-collector when we get the lock here.
                    attribute_hash = self._check_resource_cache(cache_key,
                                                                resource)
                    if attribute_hash:
                        method(resource_type, resource, *args, **kwargs)
                        self.cache.set(cache_key, attribute_hash)
                    else:
                        LOG.debug('resource cache recheck hit for '
                                  '%s %s', operation, cache_key)
                self._gnocchi_resource_lock.pop(cache_key, None)
            else:
                LOG.debug('Resource cache hit for %s %s', operation, cache_key)
        else:
            method(resource_type, resource, *args, **kwargs)

    def _check_resource_cache(self, key, resource_data):
        cached_hash = self.cache.get(key)
        attribute_hash = hash(frozenset(filter(lambda x: x[0] != "metrics",
                                               resource_data.items())))
        if not cached_hash or cached_hash != attribute_hash:
            return attribute_hash
        else:
            return None
