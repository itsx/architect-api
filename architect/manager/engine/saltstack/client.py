# -*- coding: utf-8 -*-

from django.conf import settings
from django import forms
from urllib.error import URLError
from pepper.libpepper import Pepper, PepperException
from architect.manager.client import BaseClient
from architect.manager.models import Manager, Resource
from celery.utils.log import get_logger

logger = get_logger(__name__)

DEFAULT_RESOURCES = [
    'salt_minion',
    'salt_service',
    'salt_lowstate',
    # 'salt_job',
]


class SaltStackClient(BaseClient):

    def __init__(self, **kwargs):
        super(SaltStackClient, self).__init__(**kwargs)

    def auth(self):
        status = True
        try:
            self.api = Pepper(self.metadata['auth_url'])
            self.api.login(self.metadata['username'],
                           self.metadata['password'],
                           'pam')
        except PepperException as exception:
            logger.error(exception)
            status = False
        except URLError as exception:
            logger.error(exception)
            status = False
        return status

    def update_resources(self, resources=None):
        if self.auth():
            if resources is None:
                resources = DEFAULT_RESOURCES
            for resource in resources:
                metadata = self.get_resource_metadata(resource)
                self.process_resource_metadata(resource, metadata)
                count = len(self.resources.get(resource, {}))
                logger.info("Processed {} {} resources".format(count,
                                                               resource))
            self.process_relation_metadata()

    def get_resource_status(self, kind, metadata):
        if not isinstance(metadata, dict):
            return 'unknown'
        if kind == 'salt_minion':
            if 'id' in metadata:
                return 'active'
        return 'unknown'

    def get_resource_metadata(self, kind):
        logger.info("Getting {} resources".format(kind))
        if kind == 'salt_job':
            metadata = self.api.low([{
                'client': 'runner',
                'fun': 'jobs.list_jobs',
                'arg': "search_function='[\"state.apply\", \"state.sls\"]'",
                'timeout': 60
            }]).get('return')[0]
        elif kind == 'salt_lowstate':
            metadata = self.api.low([{
                'client': 'local',
                'tgt': '*',
                'fun': 'state.show_lowstate',
                'timeout': 60
            }]).get('return')[0]
        elif kind == 'salt_minion':
            metadata = self.api.low([{
                'client': 'local',
                'tgt': '*',
                'fun': 'grains.items',
                'timeout': 60
            }]).get('return')[0]
        elif kind == 'salt_service':
            metadata = self.api.low([{
                'client': 'local',
                'tgt': '*',
                'fun': 'pillar.data',
                'timeout': 60
            }]).get('return')[0]
        else:
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        return metadata

    def process_resource_metadata(self, kind, metadata):
        if kind == 'salt_event':
            manager = Manager.objects.get(name=metadata.get('manager'))
            roles = []
            if isinstance(metadata.get('return'), (list, tuple)):
                return
            for datum_name, datum in metadata.get('return', {}).items():
                try:
                    uid = '{}|{}'.format(metadata['id'], datum['__id__'])
                except KeyError as exception:
                    logger.error('No key {} in {}'.format(exception, datum))
                    continue
                try:
                    lowstate = Resource.objects.get(uid=uid, manager=manager)
                except Resource.DoesNotExist:
                    logger.error('No salt_lowstate resource '
                                 'with UID {} found'.format(uid))
                    continue
                to_save = False

                if 'apply' not in lowstate.metadata:
                    lowstate.metadata['apply'] = {}
                    lowstate.metadata['apply'][metadata.get('jid')] = datum
                    if datum['result']:
                        lowstate.status = 'active'
                    else:
                        lowstate.status = 'error'
                    to_save = True
                else:
                    if metadata.get('jid') not in lowstate.metadata['apply']:
                        lowstate.metadata['apply'][metadata.get('jid')] = datum
                        if datum['result']:
                            lowstate.status = 'active'
                        else:
                            lowstate.status = 'error'
                        to_save = True
                if to_save:
                    lowstate.save()
                    role_parts = lowstate.metadata['__sls__'].split('.')
                    role_name = "{}-{}".format(role_parts[0],
                                               role_parts[1])
                    roles.append(role_name)

            for role_name in set(roles):
                uid = '{}|{}'.format(metadata['id'], role_name)
                try:
                    service = Resource.objects.get(uid=uid, manager=manager)
                except Resource.DoesNotExist:
                    logger.error('No salt_service resource '
                                 'with UID {} found'.format(uid))
                    continue
                errors = 0
                unknown = 0
                to_save = False
                lowstate_links = service.source.filter(kind='state_of_service')
                for lowstate_link in lowstate_links:
                    if lowstate_link.target.status == 'error':
                        errors += 1
                    elif lowstate_link.target.status == 'unknown':
                        unknown += 1
                if errors > 0 and service.status != 'error':
                    service.status = 'error'
                    to_save = True
                if unknown > 0 and service.status != 'unknown':
                    service.status = 'build'
                    to_save = True
                elif service.status != 'active':
                    service.status = 'active'
                    to_save = True
                if to_save:
                    service.save()
        elif kind == 'salt_job':
            for job_id, job in metadata.items():
                if not isinstance(job, dict):
                    continue
                if job['Function'] in ['state.apply', 'state.sls']:
                    result = self.api.lookup_jid(job_id).get('return')[0]
                    job['Result'] = result
                    self._create_resource(job_id,
                                          job['Function'],
                                          'salt_job',
                                          metadata=job)
                    self._create_resource(job['User'],
                                          job['User'].replace('sudo_', ''),
                                          'salt_user',
                                          metadata={})
        elif kind == 'salt_lowstate':
            for minion_id, low_states in metadata.items():
                if not isinstance(low_states, list):
                    continue
                for low_state in low_states:
                    if not isinstance(low_state, dict):
                        logger.error('Salt lowtate {} parsing problem on '
                                     '{}'.format(low_state, minion_id))
                        continue
                    low_state['minion'] = minion_id
                    self._create_resource('{}|{}'.format(minion_id,
                                                         low_state['__id__']),
                                          '{} {}'.format(low_state['state'],
                                                         low_state['__id__']),
                                          'salt_lowstate',
                                          metadata=low_state)
        elif kind == 'salt_minion':
            self._create_resource('salt-master',
                                  'salt-master',
                                  'salt_master',
                                  metadata={})
            for minion_id, minion_data in metadata.items():
                self._create_resource(minion_id,
                                      minion_id,
                                      'salt_minion',
                                      metadata={'grains': minion_data})
        elif kind == 'salt_service':
            for minion_id, minion_data in metadata.items():
                if not isinstance(minion_data, dict):
                    continue
                for service_name, service in minion_data.items():
                    if service_name not in settings.SALT_SERVICE_BLACKLIST:
                        if not isinstance(service, dict):
                            logger.error('Salt service {} parsing problem: '
                                         '{} on {}'.format(service_name, service, minion_id))
                            continue
                        self._create_resource('{}|{}'.format(minion_id, service_name),
                                              service_name,
                                              'salt_service',
                                              metadata={'pillar': service})

    def process_relation_metadata(self):
        # Define relationships between minions and master
        for resource_id, resource in self.resources.get('salt_minion',
                                                        {}).items():
            self._create_relation(
                'controlled_by_master',
                resource_id,
                'salt-master')

        # Define relationships between services and minions
        for resource_id, resource in self.resources.get('salt_service',
                                                        {}).items():
            self._create_relation(
                'runs_on_minion',
                resource_id,
                resource_id.split('|')[0])

        # Define relationships between lowstates and services
        for resource_id, resource in self.resources.get('salt_lowstate',
                                                        {}).items():
            split_service = resource['metadata']['__sls__'].split('.')
            self._create_relation(
                'state_of_service',
                resource_id,
                '{}|{}'.format(resource['metadata']['minion'],
                               split_service[0]))

        for resource_id, resource in self.resources.get('salt_job',
                                                        {}).items():
            self._create_relation(
                'action_by_user',
                resource_id,
                resource['metadata']['User'])
            for minion_id, result in resource['metadata'].get('Result',
                                                              {}).items():
                self._create_relation(
                    'applied_on_minion',
                    resource_id,
                    minion_id)
                if type(result) is list:
                    logger.error(result[0])
                else:
                    for state_id, state in result.items():
                        if '__id__' in state:
                            result_id = '{}|{}'.format(minion_id,
                                                       state['__id__'])
                            self._create_relation(
                                'applied_lowstate',
                                resource_id,
                                result_id)

    def get_resource_action_fields(self, resource, action):
        fields = {}
        if resource.kind == 'salt_minion':
            if action == 'run_module':
                fields['function'] = forms.CharField(label='Module function',
                                                     initial='cmd.run')
                fields['arguments'] = forms.CharField(widget=forms.Textarea(attrs={'rows': 3, 'cols': 40}),
                                                      required=False,
                                                      label='Function arguments')
        elif resource.kind == 'salt_master':
            if action == 'generate_key':
                fields['minion_id'] = forms.CharField(label='Minion ID')
                fields['force'] = forms.BooleanField(label='Force create', required=False)
        return fields

    def process_resource_action(self, resource, action, data):
        if resource.kind == 'salt_minion':
            if action == 'run_module':
                if self.auth():
                    run_metadata = {
                        'client': 'local',
                        'tgt': resource.uid,
                        'fun': data['function'],
                        'timeout': 60
                    }
                    if data['arguments'] != '':
                        run_metadata['arg'] = [s.strip() for s in data['arguments'].splitlines()]
                    metadata = self.api.low([run_metadata]).get('return')[0]
        elif resource.kind == 'salt_master':
            if action == 'generate_key':
                if self.auth():
                    run_metadata = {
                        'client': 'wheel',
                        'fun': 'key.gen_accept',
                        'id_': data['minion_id'],
                        'force': data['force'],
                    }
                    data = self.api.low([run_metadata]).get('return')
                    metadata = data[0]['data']['return']
        else:
            metadata = None
        return metadata
