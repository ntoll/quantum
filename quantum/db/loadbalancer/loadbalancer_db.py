# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright 2013 OpenStack LLC.  All rights reserved
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
#

import sqlalchemy as sa
from sqlalchemy import orm
from sqlalchemy.orm import exc
from sqlalchemy.sql import expression as expr
import webob.exc as w_exc

from quantum.api.v2 import attributes
from quantum.common import exceptions as q_exc
from quantum.db import db_base_plugin_v2
from quantum.db import model_base
from quantum.db import models_v2
from quantum.extensions import loadbalancer
from quantum.extensions.loadbalancer import LoadBalancerPluginBase
from quantum.openstack.common import cfg
from quantum.openstack.common import log as logging
from quantum.openstack.common import uuidutils
from quantum.plugins.common import constants
from quantum import policy


LOG = logging.getLogger(__name__)


class SessionPersistence(model_base.BASEV2):
    vip_id = sa.Column(sa.String(36),
                       sa.ForeignKey("vips.id"),
                       primary_key=True)
    type = sa.Column(sa.Enum("SOURCE_IP",
                             "HTTP_COOKIE",
                             "APP_COOKIE",
                             name="sesssionpersistences_type"),
                     nullable=False)
    cookie_name = sa.Column(sa.String(1024))


class PoolStatistics(model_base.BASEV2):
    """Represents pool statistics """
    pool_id = sa.Column(sa.String(36), sa.ForeignKey("pools.id"),
                        primary_key=True)
    bytes_in = sa.Column(sa.Integer, nullable=False)
    bytes_out = sa.Column(sa.Integer, nullable=False)
    active_connections = sa.Column(sa.Integer, nullable=False)
    total_connections = sa.Column(sa.Integer, nullable=False)


class Vip(model_base.BASEV2, models_v2.HasId, models_v2.HasTenant):
    """Represents a v2 quantum loadbalancer vip."""
    name = sa.Column(sa.String(255))
    description = sa.Column(sa.String(255))
    subnet_id = sa.Column(sa.String(36), nullable=False)
    address = sa.Column(sa.String(64))
    port = sa.Column(sa.Integer, nullable=False)
    protocol = sa.Column(sa.Enum("HTTP", "HTTPS", name="vip_protocol"),
                         nullable=False)
    pool_id = sa.Column(sa.String(36), nullable=False)
    session_persistence = orm.relationship(SessionPersistence,
                                           uselist=False,
                                           backref="vips",
                                           cascade="all, delete-orphan")
    status = sa.Column(sa.String(16), nullable=False)
    admin_state_up = sa.Column(sa.Boolean(), nullable=False)
    connection_limit = sa.Column(sa.Integer)


class Member(model_base.BASEV2, models_v2.HasId, models_v2.HasTenant):
    """Represents a v2 quantum loadbalancer member."""
    pool_id = sa.Column(sa.String(36), sa.ForeignKey("pools.id"),
                        nullable=False)
    address = sa.Column(sa.String(64), nullable=False)
    port = sa.Column(sa.Integer, nullable=False)
    weight = sa.Column(sa.Integer, nullable=False)
    status = sa.Column(sa.String(16), nullable=False)
    admin_state_up = sa.Column(sa.Boolean(), nullable=False)


class Pool(model_base.BASEV2, models_v2.HasId, models_v2.HasTenant):
    """Represents a v2 quantum loadbalancer pool."""
    vip_id = sa.Column(sa.String(36), sa.ForeignKey("vips.id"))
    name = sa.Column(sa.String(255))
    description = sa.Column(sa.String(255))
    subnet_id = sa.Column(sa.String(36), nullable=False)
    protocol = sa.Column(sa.String(64), nullable=False)
    lb_method = sa.Column(sa.Enum("ROUND_ROBIN",
                                  "LEAST_CONNECTIONS",
                                  "SOURCE_IP",
                                  name="pools_lb_method"),
                          nullable=False)
    status = sa.Column(sa.String(16), nullable=False)
    admin_state_up = sa.Column(sa.Boolean(), nullable=False)
    stats = orm.relationship(PoolStatistics,
                             uselist=False,
                             backref="pools",
                             cascade="all, delete-orphan")
    members = orm.relationship(Member, backref="pools",
                               cascade="all, delete-orphan")
    monitors = orm.relationship("PoolMonitorAssociation", backref="pools",
                                cascade="all, delete-orphan")


class HealthMonitor(model_base.BASEV2, models_v2.HasId, models_v2.HasTenant):
    """Represents a v2 quantum loadbalancer healthmonitor."""
    type = sa.Column(sa.Enum("PING", "TCP", "HTTP", "HTTPS",
                             name="healthmontiors_type"),
                     nullable=False)
    delay = sa.Column(sa.Integer, nullable=False)
    timeout = sa.Column(sa.Integer, nullable=False)
    max_retries = sa.Column(sa.Integer, nullable=False)
    http_method = sa.Column(sa.String(16))
    url_path = sa.Column(sa.String(255))
    expected_codes = sa.Column(sa.String(64))
    status = sa.Column(sa.String(16), nullable=False)
    admin_state_up = sa.Column(sa.Boolean(), nullable=False)


class PoolMonitorAssociation(model_base.BASEV2):
    """
    Represents the many-to-many association between pool and
    healthMonitor classes
    """
    pool_id = sa.Column(sa.String(36),
                        sa.ForeignKey("pools.id"),
                        primary_key=True)
    monitor_id = sa.Column(sa.String(36),
                           sa.ForeignKey("healthmonitors.id"),
                           primary_key=True)
    monitor = orm.relationship("HealthMonitor",
                               backref="pools_poolmonitorassociations")


class LoadBalancerPluginDb(LoadBalancerPluginBase):
    """
    A class that wraps the implementation of the Quantum
    loadbalancer plugin database access interface using SQLAlchemy models.
    """

    # TODO(lcui):
    # A set of internal facility methods are borrowed from QuantumDbPluginV2
    # class and hence this is duplicate. We need to pull out those methods
    # into a seperate class which can be used by both QuantumDbPluginV2 and
    # this class (and others).
    def _get_tenant_id_for_create(self, context, resource):
        if context.is_admin and 'tenant_id' in resource:
            tenant_id = resource['tenant_id']
        elif ('tenant_id' in resource and
              resource['tenant_id'] != context.tenant_id):
            reason = _('Cannot create resource for another tenant')
            raise q_exc.AdminRequired(reason=reason)
        else:
            tenant_id = context.tenant_id
        return tenant_id

    def _fields(self, resource, fields):
        if fields:
            return dict((key, item) for key, item in resource.iteritems()
                        if key in fields)
        return resource

    def _apply_filters_to_query(self, query, model, filters):
        if filters:
            for key, value in filters.iteritems():
                column = getattr(model, key, None)
                if column:
                    query = query.filter(column.in_(value))
        return query

    def _get_collection_query(self, context, model, filters=None):
        collection = self._model_query(context, model)
        collection = self._apply_filters_to_query(collection, model, filters)
        return collection

    def _get_collection(self, context, model, dict_func, filters=None,
                        fields=None):
        query = self._get_collection_query(context, model, filters)
        return [dict_func(c, fields) for c in query.all()]

    def _get_collection_count(self, context, model, filters=None):
        return self._get_collection_query(context, model, filters).count()

    def _model_query(self, context, model):
        query = context.session.query(model)
        query_filter = None
        if not context.is_admin and hasattr(model, 'tenant_id'):
            if hasattr(model, 'shared'):
                query_filter = ((model.tenant_id == context.tenant_id) |
                                (model.shared))
            else:
                query_filter = (model.tenant_id == context.tenant_id)

        if query_filter is not None:
            query = query.filter(query_filter)
        return query

    def _get_by_id(self, context, model, id):
        query = self._model_query(context, model)
        return query.filter(model.id == id).one()

    def update_status(self, context, model, id, status):
        with context.session.begin(subtransactions=True):
            v_db = self._get_resource(context, model, id)
            v_db.update({'status': status})

    def _get_resource(self, context, model, id):
        try:
            r = self._get_by_id(context, model, id)
        except exc.NoResultFound:
            if issubclass(model, Vip):
                raise loadbalancer.VipNotFound(vip_id=id)
            elif issubclass(model, Pool):
                raise loadbalancer.PoolNotFound(pool_id=id)
            elif issubclass(model, Member):
                raise loadbalancer.MemberNotFound(member_id=id)
            elif issubclass(model, HealthMonitor):
                raise loadbalancer.HealthMonitorNotFound(monitor_id=id)
            else:
                raise
        return r

    ########################################################
    # VIP DB access
    def _make_vip_dict(self, vip, fields=None):
        res = {'id': vip['id'],
               'tenant_id': vip['tenant_id'],
               'name': vip['name'],
               'description': vip['description'],
               'subnet_id': vip['subnet_id'],
               'address': vip['address'],
               'port': vip['port'],
               'protocol': vip['protocol'],
               'pool_id': vip['pool_id'],
               'connection_limit': vip['connection_limit'],
               'admin_state_up': vip['admin_state_up'],
               'status': vip['status']}
        if vip['session_persistence']:
            res['session_persistence'] = {
                'type': vip['session_persistence']['type'],
                'cookie_name': vip['session_persistence']['cookie_name']
            }
        return self._fields(res, fields)

    def _update_pool_vip_info(self, context, pool_id, vip_id):
        pool_db = self._get_resource(context, Pool, pool_id)
        with context.session.begin(subtransactions=True):
            pool_db.update({'vip_id': vip_id})

    def _update_vip_session_persistence_info(self, context, vip_id, info):
        vip = self._get_resource(context, Vip, vip_id)

        with context.session.begin(subtransactions=True):
            # Update sessionPersistence table
            sess_qry = context.session.query(SessionPersistence)
            sesspersist_db = sess_qry.filter_by(vip_id=vip_id).first()
            if sesspersist_db:
                sesspersist_db.update(info)
            else:
                sesspersist_db = SessionPersistence(
                    type=info['type'],
                    cookie_name=info['cookie_name'],
                    vip_id=vip_id)
                context.session.add(sesspersist_db)
                # Update vip table
                vip.session_persistence = sesspersist_db
            context.session.add(vip)

    def _delete_sessionpersistence(self, context, id):
        with context.session.begin(subtransactions=True):
            sess_qry = context.session.query(SessionPersistence)
            sess_qry.filter_by(vip_id=id).delete()

    def create_vip(self, context, vip):
        v = vip['vip']
        tenant_id = self._get_tenant_id_for_create(context, v)

        with context.session.begin(subtransactions=True):
            if v['address'] is attributes.ATTR_NOT_SPECIFIED:
                address = None
            else:
                address = v['address']
            vip_db = Vip(id=uuidutils.generate_uuid(),
                         tenant_id=tenant_id,
                         name=v['name'],
                         description=v['description'],
                         subnet_id=v['subnet_id'],
                         address=address,
                         port=v['port'],
                         protocol=v['protocol'],
                         pool_id=v['pool_id'],
                         connection_limit=v['connection_limit'],
                         admin_state_up=v['admin_state_up'],
                         status=constants.PENDING_CREATE)
            vip_id = vip_db['id']

            sessionInfo = v['session_persistence']
            if sessionInfo:
                has_session_persistence = True
                sesspersist_db = SessionPersistence(
                    type=sessionInfo['type'],
                    cookie_name=sessionInfo['cookie_name'],
                    vip_id=vip_id)
                vip_db.session_persistence = sesspersist_db

            context.session.add(vip_db)
            self._update_pool_vip_info(context, v['pool_id'], vip_id)

        vip_db = self._get_resource(context, Vip, vip_id)
        return self._make_vip_dict(vip_db)

    def update_vip(self, context, id, vip):
        v = vip['vip']

        sesspersist_info = v.pop('session_persistence', None)
        with context.session.begin(subtransactions=True):
            if sesspersist_info:
                self._update_vip_session_persistence_info(context,
                                                          id,
                                                          sesspersist_info)

            vip_db = self._get_resource(context, Vip, id)
            old_pool_id = vip_db['pool_id']
            if v:
                vip_db.update(v)
                # If the pool_id is changed, we need to update
                # the associated pools
                if 'pool_id' in v:
                    self._update_pool_vip_info(context, old_pool_id, None)
                    self._update_pool_vip_info(context, v['pool_id'], id)

        return self._make_vip_dict(vip_db)

    def delete_vip(self, context, id):
        with context.session.begin(subtransactions=True):
            vip = self._get_resource(context, Vip, id)
            qry = context.session.query(Pool)
            for pool in qry.filter_by(vip_id=id).all():
                pool.update({"vip_id": None})
            context.session.delete(vip)

    def get_vip(self, context, id, fields=None):
        vip = self._get_resource(context, Vip, id)
        return self._make_vip_dict(vip, fields)

    def get_vips(self, context, filters=None, fields=None):
        return self._get_collection(context, Vip,
                                    self._make_vip_dict,
                                    filters=filters, fields=fields)

    ########################################################
    # Pool DB access
    def _make_pool_dict(self, context, pool, fields=None):
        res = {'id': pool['id'],
               'tenant_id': pool['tenant_id'],
               'name': pool['name'],
               'description': pool['description'],
               'subnet_id': pool['subnet_id'],
               'protocol': pool['protocol'],
               'vip_id': pool['vip_id'],
               'lb_method': pool['lb_method'],
               'admin_state_up': pool['admin_state_up'],
               'status': pool['status']}

        # Get the associated members
        res['members'] = [member['id'] for member in pool['members']]

        # Get the associated health_monitors
        res['health_monitors'] = [
            monitor['monitor_id'] for monitor in pool['monitors']]

        return self._fields(res, fields)

    def _update_pool_member_info(self, context, pool_id, membersInfo):
        with context.session.begin(subtransactions=True):
            member_qry = context.session.query(Member)
            for member_id in membersInfo:
                try:
                    member = member_qry.filter_by(id=member_id).one()
                    member.update({'pool_id': pool_id})
                except exc.NoResultFound:
                    raise loadbalancer.MemberNotFound(member_id=member_id)

    def _create_pool_stats(self, context, pool_id):
        # This is internal method to add pool statistics. It won't
        # be exposed to API
        stats_db = PoolStatistics(
            pool_id=pool_id,
            bytes_in=0,
            bytes_out=0,
            active_connections=0,
            total_connections=0
        )
        return stats_db

    def _delete_pool_stats(self, context, pool_id):
        # This is internal method to delete pool statistics. It won't
        # be exposed to API
        with context.session.begin(subtransactions=True):
            stats_qry = context.session.query(PoolStatistics)
            try:
                stats = stats_qry.filter_by(pool_id=pool_id).one()
            except exc.NoResultFound:
                raise loadbalancer.PoolStatsNotFound(pool_id=pool_id)
            context.session.delete(stats)

    def create_pool(self, context, pool):
        v = pool['pool']

        tenant_id = self._get_tenant_id_for_create(context, v)
        with context.session.begin(subtransactions=True):
            pool_db = Pool(id=uuidutils.generate_uuid(),
                           tenant_id=tenant_id,
                           name=v['name'],
                           description=v['description'],
                           subnet_id=v['subnet_id'],
                           protocol=v['protocol'],
                           lb_method=v['lb_method'],
                           admin_state_up=v['admin_state_up'],
                           status=constants.PENDING_CREATE)
            pool_db.stats = self._create_pool_stats(context, pool_db['id'])
            context.session.add(pool_db)

        pool_db = self._get_resource(context, Pool, pool_db['id'])
        return self._make_pool_dict(context, pool_db)

    def update_pool(self, context, id, pool):
        v = pool['pool']

        with context.session.begin(subtransactions=True):
            pool_db = self._get_resource(context, Pool, id)
            if v:
                pool_db.update(v)

        return self._make_pool_dict(context, pool_db)

    def delete_pool(self, context, id):
        # Check if the pool is in use
        vip = context.session.query(Vip).filter_by(pool_id=id).first()
        if vip:
            raise loadbalancer.PoolInUse(pool_id=id)

        with context.session.begin(subtransactions=True):
            self._delete_pool_stats(context, id)
            pool_db = self._get_resource(context, Pool, id)
            context.session.delete(pool_db)

    def get_pool(self, context, id, fields=None):
        pool = self._get_resource(context, Pool, id)
        return self._make_pool_dict(context, pool, fields)

    def get_pools(self, context, filters=None, fields=None):
        collection = self._model_query(context, Pool)
        collection = self._apply_filters_to_query(collection, Pool, filters)
        return [self._make_pool_dict(context, c, fields)
                for c in collection.all()]

    def get_stats(self, context, pool_id):
        with context.session.begin(subtransactions=True):
            pool_qry = context.session.query(Pool)
            try:
                pool = pool_qry.filter_by(id=pool_id).one()
                stats = pool['stats']
            except exc.NoResultFound:
                raise loadbalancer.PoolStatsNotFound(pool_id=pool_id)

        res = {'bytes_in': stats['bytes_in'],
               'bytes_out': stats['bytes_out'],
               'active_connections': stats['active_connections'],
               'total_connections': stats['total_connections']}
        return {'stats': res}

    def create_pool_health_monitor(self, context, health_monitor, pool_id):
        monitor_id = health_monitor['health_monitor']['id']
        with context.session.begin(subtransactions=True):
            monitor_qry = context.session.query(HealthMonitor)
            try:
                monitor = monitor_qry.filter_by(id=monitor_id).one()
                monitor.update({'pool_id': pool_id})
            except exc.NoResultFound:
                raise loadbalancer.HealthMonitorNotFound(monitor_id=monitor_id)
            try:
                qry = context.session.query(Pool)
                pool = qry.filter_by(id=pool_id).one()
            except exc.NoResultFound:
                raise loadbalancer.PoolNotFound(pool_id=pool_id)

            assoc = PoolMonitorAssociation(pool_id=pool_id,
                                           monitor_id=monitor_id)
            assoc.monitor = monitor
            pool.monitors.append(assoc)

        monitors = []
        try:
            qry = context.session.query(Pool)
            pool = qry.filter_by(id=pool_id).one()
            for monitor in pool['monitors']:
                monitors.append(monitor['monitor_id'])
        except exc.NoResultFound:
            pass

        res = {"health_monitor": monitors}
        return res

    def delete_pool_health_monitor(self, context, id, pool_id):
        with context.session.begin(subtransactions=True):
            try:
                pool_qry = context.session.query(Pool)
                pool = pool_qry.filter_by(id=pool_id).one()
            except exc.NoResultFound:
                raise loadbalancer.PoolNotFound(pool_id=pool_id)
            try:
                monitor_qry = context.session.query(PoolMonitorAssociation)
                monitor = monitor_qry.filter_by(monitor_id=id,
                                                pool_id=pool_id).one()
                pool.monitors.remove(monitor)
            except exc.NoResultFound:
                raise loadbalancer.HealthMonitorNotFound(monitor_id=id)

    def get_pool_health_monitor(self, context, id, pool_id, fields=None):
        healthmonitor = self._get_resource(context, HealthMonitor, id)
        return self._make_health_monitor_dict(healthmonitor, fields)

    ########################################################
    # Member DB access
    def _make_member_dict(self, member, fields=None):
        res = {'id': member['id'],
               'tenant_id': member['tenant_id'],
               'pool_id': member['pool_id'],
               'address': member['address'],
               'port': member['port'],
               'weight': member['weight'],
               'admin_state_up': member['admin_state_up'],
               'status': member['status']}
        return self._fields(res, fields)

    def create_member(self, context, member):
        v = member['member']
        tenant_id = self._get_tenant_id_for_create(context, v)

        with context.session.begin(subtransactions=True):
            pool = None
            try:
                qry = context.session.query(Pool)
                pool = qry.filter_by(id=v['pool_id']).one()
            except exc.NoResultFound:
                raise loadbalancer.PoolNotFound(pool_id=v['pool_id'])

            member_db = Member(id=uuidutils.generate_uuid(),
                               tenant_id=tenant_id,
                               pool_id=v['pool_id'],
                               address=v['address'],
                               port=v['port'],
                               weight=v['weight'],
                               admin_state_up=v['admin_state_up'],
                               status=constants.PENDING_CREATE)
            context.session.add(member_db)

        return self._make_member_dict(member_db)

    def update_member(self, context, id, member):
        v = member['member']
        with context.session.begin(subtransactions=True):
            member_db = self._get_resource(context, Member, id)
            old_pool_id = member_db['pool_id']
            if v:
                member_db.update(v)

        return self._make_member_dict(member_db)

    def delete_member(self, context, id):
        with context.session.begin(subtransactions=True):
            member_db = self._get_resource(context, Member, id)
            context.session.delete(member_db)

    def get_member(self, context, id, fields=None):
        member = self._get_resource(context, Member, id)
        return self._make_member_dict(member, fields)

    def get_members(self, context, filters=None, fields=None):
        return self._get_collection(context, Member,
                                    self._make_member_dict,
                                    filters=filters, fields=fields)

    ########################################################
    # HealthMonitor DB access
    def _make_health_monitor_dict(self, health_monitor, fields=None):
        res = {'id': health_monitor['id'],
               'tenant_id': health_monitor['tenant_id'],
               'type': health_monitor['type'],
               'delay': health_monitor['delay'],
               'timeout': health_monitor['timeout'],
               'max_retries': health_monitor['max_retries'],
               'http_method': health_monitor['http_method'],
               'url_path': health_monitor['url_path'],
               'expected_codes': health_monitor['expected_codes'],
               'admin_state_up': health_monitor['admin_state_up'],
               'status': health_monitor['status']}
        return self._fields(res, fields)

    def create_health_monitor(self, context, health_monitor):
        v = health_monitor['health_monitor']
        tenant_id = self._get_tenant_id_for_create(context, v)
        with context.session.begin(subtransactions=True):
            monitor_db = HealthMonitor(id=uuidutils.generate_uuid(),
                                       tenant_id=tenant_id,
                                       type=v['type'],
                                       delay=v['delay'],
                                       timeout=v['timeout'],
                                       max_retries=v['max_retries'],
                                       http_method=v['http_method'],
                                       url_path=v['url_path'],
                                       expected_codes=v['expected_codes'],
                                       admin_state_up=v['admin_state_up'],
                                       status=constants.PENDING_CREATE)
            context.session.add(monitor_db)
        return self._make_health_monitor_dict(monitor_db)

    def update_health_monitor(self, context, id, health_monitor):
        v = health_monitor['health_monitor']
        with context.session.begin(subtransactions=True):
            monitor_db = self._get_resource(context, HealthMonitor, id)
            if v:
                monitor_db.update(v)
        return self._make_health_monitor_dict(monitor_db)

    def delete_health_monitor(self, context, id):
        with context.session.begin(subtransactions=True):
            assoc_qry = context.session.query(PoolMonitorAssociation)
            pool_qry = context.session.query(Pool)
            for assoc in assoc_qry.filter_by(monitor_id=id).all():
                try:
                    pool = pool_qry.filter_by(id=assoc['pool_id']).one()
                except exc.NoResultFound:
                    raise loadbalancer.PoolNotFound(pool_id=pool['id'])
                pool.monitors.remove(assoc)
            monitor_db = self._get_resource(context, HealthMonitor, id)
            context.session.delete(monitor_db)

    def get_health_monitor(self, context, id, fields=None):
        healthmonitor = self._get_resource(context, HealthMonitor, id)
        return self._make_health_monitor_dict(healthmonitor, fields)

    def get_health_monitors(self, context, filters=None, fields=None):
        return self._get_collection(context, HealthMonitor,
                                    self._make_health_monitor_dict,
                                    filters=filters, fields=fields)
