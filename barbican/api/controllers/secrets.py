#  Licensed under the Apache License, Version 2.0 (the "License"); you may
#  not use this file except in compliance with the License. You may obtain
#  a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#  WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#  License for the specific language governing permissions and limitations
#  under the License.

import mimetypes
import urllib

import pecan

from barbican import api
from barbican.api import controllers
from barbican.api.controllers import hrefs
from barbican.common import exception
from barbican.common import resources as res
from barbican.common import utils
from barbican.common import validators
from barbican.crypto import mime_types
from barbican.model import repositories as repo
from barbican.openstack.common import gettextutils as u

LOG = utils.getLogger(__name__)


def allow_all_content_types(f):
    cfg = pecan.util._cfg(f)
    for value in mimetypes.types_map.values():
        cfg.setdefault('content_types', {})[value] = ''
    return f


def _secret_not_found():
    """Throw exception indicating secret not found."""
    pecan.abort(404, u._('Not Found. Sorry but your secret is in '
                         'another castle.'))


def _secret_already_has_data():
    """Throw exception that the secret already has data."""
    pecan.abort(409, u._("Secret already has data, cannot modify it."))


class SecretController(object):
    """Handles Secret retrieval and deletion requests."""

    def __init__(self, secret_id, crypto_manager,
                 tenant_repo=None, secret_repo=None, datum_repo=None,
                 kek_repo=None):
        LOG.debug('=== Creating SecretController ===')
        self.secret_id = secret_id
        self.crypto_manager = crypto_manager
        self.tenant_repo = tenant_repo or repo.TenantRepo()
        self.repo = secret_repo or repo.SecretRepo()
        self.datum_repo = datum_repo or repo.EncryptedDatumRepo()
        self.kek_repo = kek_repo or repo.KEKDatumRepo()

    @pecan.expose(generic=True)
    @allow_all_content_types
    @controllers.handle_exceptions(u._('Secret retrieval'))
    @controllers.handle_rbac('secret:get')
    def index(self, keystone_id):

        secret = self.repo.get(entity_id=self.secret_id,
                               keystone_id=keystone_id,
                               suppress_exception=True)
        if not secret:
            _secret_not_found()

        if controllers.is_json_request_accept(pecan.request):
            # Metadata-only response, no decryption necessary.
            pecan.override_template('json', 'application/json')
            secret_fields = mime_types.augment_fields_with_content_types(
                secret)
            return hrefs.convert_to_hrefs(keystone_id, secret_fields)
        else:
            tenant = res.get_or_create_tenant(keystone_id, self.tenant_repo)
            pecan.override_template('', pecan.request.accept.header_value)
            return self.crypto_manager.decrypt(
                pecan.request.accept.header_value,
                secret,
                tenant
            )

    @index.when(method='PUT')
    @allow_all_content_types
    @controllers.handle_exceptions(u._('Secret update'))
    @controllers.handle_rbac('secret:put')
    def on_put(self, keystone_id):

        if not pecan.request.content_type or \
                pecan.request.content_type == 'application/json':
            pecan.abort(
                415,
                u._("Content-Type of '{0}' is not supported for PUT.").format(
                    pecan.request.content_type
                )
            )

        secret = self.repo.get(entity_id=self.secret_id,
                               keystone_id=keystone_id,
                               suppress_exception=True)
        if not secret:
            _secret_not_found()

        if secret.encrypted_data:
            _secret_already_has_data()

        tenant = res.get_or_create_tenant(keystone_id, self.tenant_repo)
        content_type = pecan.request.content_type
        content_encoding = pecan.request.headers.get('Content-Encoding')

        res.create_encrypted_datum(secret,
                                   pecan.request.body,
                                   content_type,
                                   content_encoding,
                                   tenant,
                                   self.crypto_manager,
                                   self.datum_repo,
                                   self.kek_repo)

    @index.when(method='DELETE')
    @controllers.handle_exceptions(u._('Secret deletion'))
    @controllers.handle_rbac('secret:delete')
    def on_delete(self, keystone_id):

        try:
            self.repo.delete_entity_by_id(entity_id=self.secret_id,
                                          keystone_id=keystone_id)
        except exception.NotFound:
            LOG.exception('Problem deleting secret')
            _secret_not_found()


class SecretsController(object):
    """Handles Secret creation requests."""

    def __init__(self, crypto_manager,
                 tenant_repo=None, secret_repo=None,
                 tenant_secret_repo=None, datum_repo=None, kek_repo=None):
        LOG.debug('Creating SecretsController')
        self.tenant_repo = tenant_repo or repo.TenantRepo()
        self.secret_repo = secret_repo or repo.SecretRepo()
        self.tenant_secret_repo = tenant_secret_repo or repo.TenantSecretRepo()
        self.datum_repo = datum_repo or repo.EncryptedDatumRepo()
        self.kek_repo = kek_repo or repo.KEKDatumRepo()
        self.crypto_manager = crypto_manager
        self.validator = validators.NewSecretValidator()

    @pecan.expose()
    def _lookup(self, secret_id, *remainder):
        return SecretController(secret_id, self.crypto_manager,
                                self.tenant_repo, self.secret_repo,
                                self.datum_repo, self.kek_repo), remainder

    @pecan.expose(generic=True, template='json')
    @controllers.handle_exceptions(u._('Secret(s) retrieval'))
    @controllers.handle_rbac('secrets:get')
    def index(self, keystone_id, **kw):
        LOG.debug('Start secrets on_get '
                  'for tenant-ID {0}:'.format(keystone_id))

        name = kw.get('name', '')
        if name:
            name = urllib.unquote_plus(name)

        bits = kw.get('bits', 0)
        try:
            bits = int(bits)
        except ValueError:
            # as per Github issue 171, if bits is invalid then
            # the default should be used.
            bits = 0

        result = self.secret_repo.get_by_create_date(
            keystone_id,
            offset_arg=kw.get('offset', 0),
            limit_arg=kw.get('limit', None),
            name=name,
            alg=kw.get('alg'),
            mode=kw.get('mode'),
            bits=bits,
            suppress_exception=True
        )

        secrets, offset, limit, total = result

        if not secrets:
            secrets_resp_overall = {'secrets': [],
                                    'total': total}
        else:
            secret_fields = lambda s: mime_types\
                .augment_fields_with_content_types(s)
            secrets_resp = [
                hrefs.convert_to_hrefs(keystone_id, secret_fields(s))
                for s in secrets
            ]
            secrets_resp_overall = hrefs.add_nav_hrefs(
                'secrets', keystone_id, offset, limit, total,
                {'secrets': secrets_resp}
            )
            secrets_resp_overall.update({'total': total})

        return secrets_resp_overall

    @index.when(method='POST', template='json')
    @controllers.handle_exceptions(u._('Secret creation'))
    @controllers.handle_rbac('secrets:post')
    def on_post(self, keystone_id):
        LOG.debug('Start on_post for tenant-ID {0}:...'.format(keystone_id))

        data = api.load_body(pecan.request, validator=self.validator)
        tenant = res.get_or_create_tenant(keystone_id, self.tenant_repo)

        new_secret = res.create_secret(data, tenant, self.crypto_manager,
                                       self.secret_repo,
                                       self.tenant_secret_repo,
                                       self.datum_repo,
                                       self.kek_repo)

        pecan.response.status = 201
        pecan.response.headers['Location'] = '/{0}/secrets/{1}'.format(
            keystone_id, new_secret.id
        )
        url = hrefs.convert_secret_to_href(keystone_id, new_secret.id)
        LOG.debug('URI to secret is {0}'.format(url))
        return {'secret_ref': url}
