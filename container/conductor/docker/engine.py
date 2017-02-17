# -*- coding: utf-8 -*-
from __future__ import absolute_import

import logging

logger = logging.getLogger(__name__)

import datetime
import os
import tarfile
import json
import base64
import functools

from ..engine import BaseEngine
from .. import utils
from .. import logmux
from .importer import DockerfileImport

try:
    import docker
    from docker import errors as docker_errors
except ImportError:
    raise ImportError('Use of this engine requires you "pip install \'docker>=2.1\'" first.')

TEMPLATES_PATH = os.path.normpath(
    os.path.join(
        os.path.dirname(__file__),
        'templates'))

FILES_PATH = os.path.normpath(
    os.path.join(
        os.path.dirname(__file__),
        'files'))

DOCKER_VERSION = '1.13.1'

def log_runs(fn):
    @functools.wraps(fn)
    def __wrapped__(self, *args, **kwargs):
        logger.debug(u'Call: %s.%s(args=%s, kwargs=%s)',
                     type(self).__name__,
                     fn.__name__,
                     unicode(args),
                     unicode(kwargs))
        return fn(self, *args, **kwargs)
    return __wrapped__

class Engine(BaseEngine):

    # Capabilities of engine implementations
    CAP_BUILD_CONDUCTOR = True
    CAP_BUILD = True
    CAP_RUN = True
    CAP_DEPLOY = True

    display_name = u'Docker\u2122 daemon'

    _client = None

    FINGERPRINT_LABEL_KEY = 'com.ansible.container.fingerprint'
    LAYER_COMMENT = 'Built with Ansible Container (https://github.com/ansible/ansible-container)'

    @property
    def client(self):
        if not self._client:
            self._client = docker.from_env()
        return self._client

    @property
    def ansible_args(self):
        """Additional commandline arguments necessary for ansible-playbook runs."""
        return u'-c docker'

    def container_name_for_service(self, service_name):
        return u'%s_%s' % (self.project_name, service_name)

    def image_name_for_service(self, service_name):
        return u'%s-%s' % (self.project_name, service_name)

    def run_kwargs_for_service(self, service_name):
        to_return = self.services[service_name].copy()
        # to_return['name'] = self.container_name_for_service(service_name)
        for key in ['from', 'roles']:
            to_return.pop(key)
        return to_return

    @log_runs
    def run_container(self,
                      image_id,
                      service_name,
                      **kwargs):
        """Run a particular container. The kwargs argument contains individual
        parameter overrides from the service definition."""
        run_kwargs = self.run_kwargs_for_service(service_name)
        run_kwargs.update(kwargs)
        logger.debug('Docker run: image=%s, params=%s', image_id, run_kwargs)

        container_obj = self.client.containers.run(
            image=image_id,
            detach=True,
            **run_kwargs
        )

        log_iter = container_obj.logs(stdout=True, stderr=True, stream=True)
        mux = logmux.LogMultiplexer()
        mux.add_iterator(log_iter, logger)
        return container_obj.id

    @log_runs
    def run_conductor(self, command, config, base_path, params):
        image_id = self.get_latest_image_id_for_service('conductor')
        serialized_params = base64.encodestring(json.dumps(params))
        serialized_config = base64.encodestring(json.dumps(config))
        volumes = {base_path: {'bind': '/src', 'mode': 'ro'}}
        environ = {}
        if os.environ.get('DOCKER_HOST'):
            environ['DOCKER_HOST'] = os.environ['DOCKER_HOST']
            if os.environ.get('DOCKER_CERT_PATH'):
                environ['DOCKER_CERT_PATH'] = '/etc/docker'
                volumes[os.environ['DOCKER_CERT_PATH']] = {'bind': '/etc/docker',
                                                           'mode': 'ro'}
            if os.environ.get('DOCKER_TLS_VERIFY'):
                environ['DOCKER_TLS_VERIFY'] = os.environ['DOCKER_TLS_VERIFY']
        else:
            environ['DOCKER_HOST'] = 'unix:///var/run/docker.sock'
            volumes['/var/run/docker.sock'] = {'bind': '/var/run/docker.sock',
                                               'mode': 'rw'}

        if params.get('devel'):
            from container import conductor
            conductor_path = os.path.dirname(conductor.__file__)
            volumes[conductor_path] = {'bind': '/_ansible/conductor/conductor', 'mode': 'rw'}

        run_kwargs = dict(
            name=self.container_name_for_service('conductor'),
            command=['conductor',
                     command,
                     '--project-name', self.project_name,
                     '--engine', __name__.rsplit('.', 2)[-2],
                     '--params', serialized_params,
                     '--config', serialized_config,
                     '--encoding', 'b64json'],
            detach=True,
            user='root',
            volumes=volumes,
            environment=environ,
        )

        logger.debug('Docker run: image=%s, params=%s', image_id, run_kwargs)

        container_obj = self.client.containers.run(
            image_id,
            **run_kwargs
        )
        log_iter = container_obj.logs(stdout=True, stderr=True, stream=True)
        mux = logmux.LogMultiplexer()
        mux.add_iterator(log_iter, logger)
        return container_obj.id

    def service_is_running(self, service):
        try:
            container = self.client.containers.get(self.container_name_for_service(service))
            return container.status == 'running' and container.id
        except docker_errors.NotFound:
            return False

    def stop_container(self, container_id, forcefully=False):
        try:
            container = self.client.containers.get(container_id)
        except docker_errors.APIError:
            pass
        else:
            if forcefully:
                container.kill()
            else:
                container.stop(timeout=60)

    def restart_all_containers(self):
        raise NotImplementedError()

    def inspect_container(self, container_id):
        try:
            return self.client.api.inspect_container(container_id)
        except docker_errors.APIError:
            return None

    def delete_container(self, container_id):
        try:
            container = self.client.containers.get(container_id)
        except docker_errors.APIError:
            pass
        else:
            container.remove()

    def get_container_id_for_service(self, service_name):
        try:
            container = self.client.containers.get(self.container_name_for_service(service_name))
        except docker_errors.NotFound:
            return None
        else:
            return container.id

    def get_image_id_by_fingerprint(self, fingerprint):
        try:
            image, = self.client.images.list(
                all=True,
                filters=dict(label='%s=%s' % (self.FINGERPRINT_LABEL_KEY,
                                              fingerprint)))
        except ValueError:
            return None
        else:
            return image.id

    def get_image_id_by_tag(self, tag):
        try:
            image = self.client.images.get(tag)
            return image.id
        except docker_errors.ImageNotFound:
            return None

    def get_latest_image_id_for_service(self, service_name):
        try:
            image = self.client.images.get(
                '%s:latest' % self.image_name_for_service(service_name))
        except docker_errors.ImageNotFound:
            return None
        else:
            return image.id

    @log_runs
    def commit_role_as_layer(self,
                             container_id,
                             service_name,
                             fingerprint,
                             metadata):
        container = self.client.containers.get(container_id)
        image_name = self.image_name_for_service(service_name)
        image_version = datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')
        image_config = utils.metadata_to_image_config(metadata)
        image_config['Labels'][self.FINGERPRINT_LABEL_KEY] = fingerprint
        commit_data = dict(repository=image_name,
            tag=image_version,
            message=self.LAYER_COMMENT,
            conf=image_config)
        logger.debug('Committing data: %s', commit_data)
        return container.commit(**commit_data)

    def generate_orchestration_playbook(self, repository_data=None):
        """If repository_data is specified, presume to pull images from that
        repository. If not, presume the images are already present."""
        # FIXME: Implement me.
        raise NotImplementedError()

    def push_image(self, image_id, service_name, repository_data):
        # FIXME: Implement me.
        raise NotImplementedError()

    @log_runs
    def build_conductor_image(self, base_path, base_image, cache=True):
        with utils.make_temp_dir() as temp_dir:
            logger.info('Building Docker Engine context...')
            tarball_path = os.path.join(temp_dir, 'context.tar')
            tarball_file = open(tarball_path, 'wb')
            tarball = tarfile.TarFile(fileobj=tarball_file,
                                      mode='w')
            source_dir = os.path.normpath(base_path)

            for filename in ['ansible.cfg', 'ansible-requirements.txt',
                             'requirements.yml']:
                file_path = os.path.join(source_dir, filename)
                if os.path.exists(filename):
                    tarball.add(file_path,
                                arcname=os.path.join('build-src', filename))
            # Make an empty file just to make sure the build-src dir has something
            open(os.path.join(temp_dir, '.touch'), 'w')
            tarball.add(os.path.join(temp_dir, '.touch'), arcname='build-src/.touch')

            tarball.add(os.path.join(FILES_PATH, 'get-pip.py'),
                        arcname='contrib/get-pip.py')

            tarball.add(utils.conductor_dir, arcname='conductor-src/conductor')
            tarball.add(os.path.join(os.path.dirname(utils.conductor_dir),
                                     'conductor-setup.py'),
                        arcname='conductor-src/setup.py')
            tarball.add(os.path.join(os.path.dirname(utils.conductor_dir),
                                     'conductor-requirements.txt'),
                        arcname='conductor-src/requirements.txt')

            utils.jinja_render_to_temp(TEMPLATES_PATH,
                                       'conductor-dockerfile.j2', temp_dir,
                                       'Dockerfile',
                                       conductor_base=base_image,
                                       docker_version=DOCKER_VERSION)
            tarball.add(os.path.join(temp_dir, 'Dockerfile'),
                        arcname='Dockerfile')

            #for context_file in ['builder.sh', 'ansible-container-inventory.py',
            #                     'ansible.cfg', 'wait_on_host.py', 'ac_galaxy.py']:
            #    tarball.add(os.path.join(TEMPLATES_PATH, context_file),
            #                arcname=context_file)

            logger.debug('Context manifest:')
            for tarinfo_obj in tarball.getmembers():
                logger.debug(' - %s (%s bytes)', tarinfo_obj.name, tarinfo_obj.size)
            tarball.close()
            tarball_file.close()
            tarball_file = open(tarball_path, 'rb')
            logger.info('Starting Docker build of Ansible Container Conductor image (please be patient)...')
            # FIXME: Error out properly if build of conductor fails.
            if self.debug:
                for line in self.client.api.build(fileobj=tarball_file,
                                                  custom_context=True,
                                                  tag=self.image_name_for_service('conductor'),
                                                  rm=True,
                                                  nocache=not cache):
                    try:
                        line_json = json.loads(line)
                        if 'stream' in line_json:
                            line = line_json['stream']
                    except ValueError:
                        pass
                    logger.debug(line)
                return self.get_latest_image_id_for_service('conductor')
            else:
                image = self.client.images.build(fileobj=tarball_file,
                                                 custom_context=True,
                                                 tag=self.image_name_for_service('conductor'),
                                                 rm=True,
                                                 nocache=not cache)
                return image.id

    def get_runtime_volume_id(self):
        try:
            container_data = self.client.api.inspect_container(
                self.container_name_for_service('conductor')
            )
        except docker_errors.APIError:
            raise ValueError('Conductor container not found.')
        mounts = container_data['Mounts']
        try:
            usr_mount, = [mount for mount in mounts if mount['Destination'] == '/usr']
        except ValueError:
            raise ValueError('Runtime volume not found on Conductor')
        return usr_mount['Name']

    def import_project(self, base_path):
        dfi = DockerfileImport(base_path,
                               self.project_name)
        dfi.assert_dockerfile_exists()
        dfi.create_role_template()
        dfi.add_role_tasks()

        logger.debug(json.dumps(dfi.environment_vars))
        logger.debug("workdir: {}".format(dfi.workdir))

        # TODO
        # dfi.create_container_yaml()
