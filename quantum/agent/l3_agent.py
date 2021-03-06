"""
# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright 2012 Nicira Networks, Inc.  All rights reserved.
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
# @author: Dan Wendlandt, Nicira, Inc
#
"""

import sys

import eventlet
from eventlet import semaphore
import netaddr

from quantum.agent.common import config
from quantum.agent.linux import external_process
from quantum.agent.linux import interface
from quantum.agent.linux import ip_lib
from quantum.agent.linux import iptables_manager
from quantum.agent.linux import utils
from quantum.common import constants as l3_constants
from quantum.common import topics
from quantum import context
from quantum import manager
from quantum.openstack.common import cfg
from quantum.openstack.common import importutils
from quantum.openstack.common import log as logging
from quantum.openstack.common import periodic_task
from quantum.openstack.common.rpc import common as rpc_common
from quantum.openstack.common.rpc import proxy
from quantum.openstack.common import service
from quantum import service as quantum_service


LOG = logging.getLogger(__name__)
NS_PREFIX = 'qrouter-'
INTERNAL_DEV_PREFIX = 'qr-'
EXTERNAL_DEV_PREFIX = 'qg-'


class L3PluginApi(proxy.RpcProxy):
    """Agent side of the l3 agent RPC API.

    API version history:
        1.0 - Initial version.

    """

    BASE_RPC_API_VERSION = '1.0'

    def __init__(self, topic, host):
        super(L3PluginApi, self).__init__(
            topic=topic, default_version=self.BASE_RPC_API_VERSION)
        self.host = host

    def get_routers(self, context, fullsync=True, router_id=None):
        """Make a remote process call to retrieve the sync data for routers."""
        router_ids = [router_id] if router_id else None
        return self.call(context,
                         self.make_msg('sync_routers', host=self.host,
                                       fullsync=fullsync,
                                       router_ids=router_ids),
                         topic=self.topic)

    def get_external_network_id(self, context):
        """Make a remote process call to retrieve the external network id.

        @raise common.RemoteError: with TooManyExternalNetworks
                                   as exc_type if there are
                                   more than one external network
        """
        return self.call(context,
                         self.make_msg('get_external_network_id',
                                       host=self.host),
                         topic=self.topic)


class RouterInfo(object):

    def __init__(self, router_id, root_helper, use_namespaces, router=None):
        self.router_id = router_id
        self.ex_gw_port = None
        self.internal_ports = []
        self.floating_ips = []
        self.root_helper = root_helper
        self.use_namespaces = use_namespaces
        self.router = router
        self.iptables_manager = iptables_manager.IptablesManager(
            root_helper=root_helper,
            #FIXME(danwent): use_ipv6=True,
            namespace=self.ns_name())

    def ns_name(self):
        if self.use_namespaces:
            return NS_PREFIX + self.router_id


class L3NATAgent(manager.Manager):

    OPTS = [
        cfg.StrOpt('root_helper', default='sudo'),
        cfg.StrOpt('external_network_bridge', default='br-ex',
                   help="Name of bridge used for external network traffic."),
        cfg.StrOpt('interface_driver',
                   help="The driver used to manage the virtual interface."),
        cfg.IntOpt('metadata_port',
                   default=9697,
                   help="TCP Port used by Quantum metadata namespace proxy."),
        cfg.IntOpt('send_arp_for_ha',
                   default=3,
                   help="Send this many gratuitous ARPs for HA setup, "
                        "set it below or equal to 0 to disable this feature."),
        cfg.BoolOpt('use_namespaces', default=True,
                    help="Allow overlapping IP."),
        cfg.StrOpt('router_id', default='',
                   help="If namespaces is disabled, the l3 agent can only"
                        " confgure a router that has the matching router ID."),
        cfg.BoolOpt('handle_internal_only_routers',
                    default=True,
                    help="Agent should implement routers with no gateway"),
        cfg.StrOpt('gateway_external_network_id', default='',
                   help="UUID of external network for routers implemented "
                        "by the agents."),
        cfg.StrOpt('l3_agent_manager',
                   default='quantum.agent.l3_agent.L3NATAgent'),
    ]

    def __init__(self, host, conf=None):
        if conf:
            self.conf = conf
        else:
            self.conf = cfg.CONF
        self.router_info = {}

        if not self.conf.interface_driver:
            LOG.error(_('An interface driver must be specified'))
            sys.exit(1)
        try:
            self.driver = importutils.import_object(self.conf.interface_driver,
                                                    self.conf)
        except:
            LOG.exception(_("Error importing interface driver '%s'"
                            % self.conf.interface_driver))
            sys.exit(1)
        self.plugin_rpc = L3PluginApi(topics.PLUGIN, host)
        self.fullsync = True
        self.sync_sem = semaphore.Semaphore(1)
        if self.conf.use_namespaces:
            self._destroy_all_router_namespaces()
        super(L3NATAgent, self).__init__(host=self.conf.host)

    def _destroy_all_router_namespaces(self):
        """Destroy all router namespaces on the host to eliminate
        all stale linux devices, iptables rules, and namespaces.
        """
        root_ip = ip_lib.IPWrapper(self.conf.root_helper)
        for ns in root_ip.get_namespaces(self.conf.root_helper):
            if ns.startswith(NS_PREFIX):
                try:
                    self._destroy_router_namespace(ns)
                except:
                    LOG.exception(_("Failed deleting namespace '%s'") % ns)

    def _destroy_router_namespace(self, namespace):
        ns_ip = ip_lib.IPWrapper(self.conf.root_helper,
                                 namespace=namespace)
        for d in ns_ip.get_devices(exclude_loopback=True):
            if d.name.startswith(INTERNAL_DEV_PREFIX):
                # device is on default bridge
                self.driver.unplug(d.name, namespace=namespace,
                                   prefix=INTERNAL_DEV_PREFIX)
            elif d.name.startswith(EXTERNAL_DEV_PREFIX):
                self.driver.unplug(d.name,
                                   bridge=self.conf.external_network_bridge,
                                   namespace=namespace,
                                   prefix=EXTERNAL_DEV_PREFIX)
        #(TODO) Address the failure for the deletion of the namespace

    def _create_router_namespace(self, ri):
            ip_wrapper_root = ip_lib.IPWrapper(self.conf.root_helper)
            ip_wrapper = ip_wrapper_root.ensure_namespace(ri.ns_name())
            ip_wrapper.netns.execute(['sysctl', '-w', 'net.ipv4.ip_forward=1'])

    def _fetch_external_net_id(self):
        """Find UUID of single external network for this agent"""
        if self.conf.gateway_external_network_id:
            return self.conf.gateway_external_network_id
        try:
            return self.plugin_rpc.get_external_network_id(
                context.get_admin_context())
        except rpc_common.RemoteError as e:
            if e.exc_type == 'TooManyExternalNetworks':
                msg = _(
                    "The 'gateway_external_network_id' must be configured"
                    " if Quantum has more than one external network.")
                raise Exception(msg)
            else:
                raise

    def _router_added(self, router_id, router=None):
        ri = RouterInfo(router_id, self.conf.root_helper,
                        self.conf.use_namespaces, router)
        self.router_info[router_id] = ri
        if self.conf.use_namespaces:
            self._create_router_namespace(ri)
        for c, r in self.metadata_filter_rules():
            ri.iptables_manager.ipv4['filter'].add_rule(c, r)
        for c, r in self.metadata_nat_rules():
            ri.iptables_manager.ipv4['nat'].add_rule(c, r)
        ri.iptables_manager.apply()
        self._spawn_metadata_proxy(ri)

    def _router_removed(self, router_id):
        ri = self.router_info[router_id]
        for c, r in self.metadata_filter_rules():
            ri.iptables_manager.ipv4['filter'].remove_rule(c, r)
        for c, r in self.metadata_nat_rules():
            ri.iptables_manager.ipv4['nat'].remove_rule(c, r)
        ri.iptables_manager.apply()
        self._destroy_metadata_proxy(ri)
        del self.router_info[router_id]
        self._destroy_router_namespace(ri.ns_name())

    def _spawn_metadata_proxy(self, router_info):
        def callback(pid_file):
            return ['quantum-ns-metadata-proxy',
                    '--pid_file=%s' % pid_file,
                    '--router_id=%s' % router_info.router_id,
                    '--state_path=%s' % self.conf.state_path]

        pm = external_process.ProcessManager(
            self.conf,
            router_info.router_id,
            self.conf.root_helper,
            router_info.ns_name())
        pm.enable(callback)

    def _destroy_metadata_proxy(self, router_info):
        pm = external_process.ProcessManager(
            self.conf,
            router_info.router_id,
            self.conf.root_helper,
            router_info.ns_name())
        pm.disable()

    def _set_subnet_info(self, port):
        ips = port['fixed_ips']
        if not ips:
            raise Exception(_("Router port %s has no IP address") % port['id'])
        if len(ips) > 1:
            LOG.error(_("Ignoring multiple IPs on router port %s") %
                      port['id'])
        prefixlen = netaddr.IPNetwork(port['subnet']['cidr']).prefixlen
        port['ip_cidr'] = "%s/%s" % (ips[0]['ip_address'], prefixlen)

    def process_router(self, ri):

        ex_gw_port = self._get_ex_gw_port(ri)
        internal_ports = ri.router.get(l3_constants.INTERFACE_KEY, [])
        existing_port_ids = set([p['id'] for p in ri.internal_ports])
        current_port_ids = set([p['id'] for p in internal_ports
                                if p['admin_state_up']])
        new_ports = [p for p in internal_ports if
                     p['id'] in current_port_ids and
                     p['id'] not in existing_port_ids]
        old_ports = [p for p in ri.internal_ports if
                     p['id'] not in current_port_ids]

        for p in new_ports:
            self._set_subnet_info(p)
            ri.internal_ports.append(p)
            self.internal_network_added(ri, ex_gw_port,
                                        p['network_id'], p['id'],
                                        p['ip_cidr'], p['mac_address'])

        for p in old_ports:
            ri.internal_ports.remove(p)
            self.internal_network_removed(ri, ex_gw_port, p['id'],
                                          p['ip_cidr'])

        internal_cidrs = [p['ip_cidr'] for p in ri.internal_ports]

        if ex_gw_port and not ri.ex_gw_port:
            self._set_subnet_info(ex_gw_port)
            self.external_gateway_added(ri, ex_gw_port, internal_cidrs)
        elif not ex_gw_port and ri.ex_gw_port:
            self.external_gateway_removed(ri, ri.ex_gw_port,
                                          internal_cidrs)

        if ri.ex_gw_port or ex_gw_port:
            self.process_router_floating_ips(ri, ex_gw_port)

        ri.ex_gw_port = ex_gw_port

    def process_router_floating_ips(self, ri, ex_gw_port):
        floating_ips = ri.router.get(l3_constants.FLOATINGIP_KEY, [])
        existing_floating_ip_ids = set([fip['id'] for fip in ri.floating_ips])
        cur_floating_ip_ids = set([fip['id'] for fip in floating_ips])

        id_to_fip_map = {}

        for fip in floating_ips:
            if fip['port_id']:
                if fip['id'] not in existing_floating_ip_ids:
                    ri.floating_ips.append(fip)
                    self.floating_ip_added(ri, ex_gw_port,
                                           fip['floating_ip_address'],
                                           fip['fixed_ip_address'])

                # store to see if floatingip was remapped
                id_to_fip_map[fip['id']] = fip

        floating_ip_ids_to_remove = (existing_floating_ip_ids -
                                     cur_floating_ip_ids)
        for fip in ri.floating_ips:
            if fip['id'] in floating_ip_ids_to_remove:
                ri.floating_ips.remove(fip)
                self.floating_ip_removed(ri, ri.ex_gw_port,
                                         fip['floating_ip_address'],
                                         fip['fixed_ip_address'])
            else:
                # handle remapping of a floating IP
                new_fip = id_to_fip_map[fip['id']]
                new_fixed_ip = new_fip['fixed_ip_address']
                existing_fixed_ip = fip['fixed_ip_address']
                if (new_fixed_ip and existing_fixed_ip and
                        new_fixed_ip != existing_fixed_ip):
                    floating_ip = fip['floating_ip_address']
                    self.floating_ip_removed(ri, ri.ex_gw_port,
                                             floating_ip, existing_fixed_ip)
                    self.floating_ip_added(ri, ri.ex_gw_port,
                                           floating_ip, new_fixed_ip)
                    ri.floating_ips.remove(fip)
                    ri.floating_ips.append(new_fip)

    def _get_ex_gw_port(self, ri):
        return ri.router.get('gw_port')

    def _send_gratuitous_arp_packet(self, ri, interface_name, ip_address):
        if self.conf.send_arp_for_ha > 0:
            arping_cmd = ['arping', '-A', '-U',
                          '-I', interface_name,
                          '-c', self.conf.send_arp_for_ha,
                          ip_address]
            try:
                if self.conf.use_namespaces:
                    ip_wrapper = ip_lib.IPWrapper(self.conf.root_helper,
                                                  namespace=ri.ns_name())
                    ip_wrapper.netns.execute(arping_cmd, check_exit_code=True)
                else:
                    utils.execute(arping_cmd, check_exit_code=True,
                                  root_helper=self.conf.root_helper)
            except Exception as e:
                LOG.error(_("Failed sending gratuitous ARP: %s") % str(e))

    def get_internal_device_name(self, port_id):
        return (INTERNAL_DEV_PREFIX + port_id)[:self.driver.DEV_NAME_LEN]

    def get_external_device_name(self, port_id):
        return (EXTERNAL_DEV_PREFIX + port_id)[:self.driver.DEV_NAME_LEN]

    def external_gateway_added(self, ri, ex_gw_port, internal_cidrs):

        interface_name = self.get_external_device_name(ex_gw_port['id'])
        ex_gw_ip = ex_gw_port['fixed_ips'][0]['ip_address']
        if not ip_lib.device_exists(interface_name,
                                    root_helper=self.conf.root_helper,
                                    namespace=ri.ns_name()):
            self.driver.plug(ex_gw_port['network_id'],
                             ex_gw_port['id'], interface_name,
                             ex_gw_port['mac_address'],
                             bridge=self.conf.external_network_bridge,
                             namespace=ri.ns_name(),
                             prefix=EXTERNAL_DEV_PREFIX)
        self.driver.init_l3(interface_name, [ex_gw_port['ip_cidr']],
                            namespace=ri.ns_name())
        ip_address = ex_gw_port['ip_cidr'].split('/')[0]
        self._send_gratuitous_arp_packet(ri, interface_name, ip_address)

        gw_ip = ex_gw_port['subnet']['gateway_ip']
        if ex_gw_port['subnet']['gateway_ip']:
            cmd = ['route', 'add', 'default', 'gw', gw_ip]
            if self.conf.use_namespaces:
                ip_wrapper = ip_lib.IPWrapper(self.conf.root_helper,
                                              namespace=ri.ns_name())
                ip_wrapper.netns.execute(cmd, check_exit_code=False)
            else:
                utils.execute(cmd, check_exit_code=False,
                              root_helper=self.conf.root_helper)

        for (c, r) in self.external_gateway_nat_rules(ex_gw_ip,
                                                      internal_cidrs,
                                                      interface_name):
            ri.iptables_manager.ipv4['nat'].add_rule(c, r)
        ri.iptables_manager.apply()

    def external_gateway_removed(self, ri, ex_gw_port, internal_cidrs):

        interface_name = self.get_external_device_name(ex_gw_port['id'])
        if ip_lib.device_exists(interface_name,
                                root_helper=self.conf.root_helper,
                                namespace=ri.ns_name()):
            self.driver.unplug(interface_name,
                               bridge=self.conf.external_network_bridge,
                               namespace=ri.ns_name(),
                               prefix=EXTERNAL_DEV_PREFIX)

        ex_gw_ip = ex_gw_port['fixed_ips'][0]['ip_address']
        for c, r in self.external_gateway_nat_rules(ex_gw_ip, internal_cidrs,
                                                    interface_name):
            ri.iptables_manager.ipv4['nat'].remove_rule(c, r)
        ri.iptables_manager.apply()

    def metadata_filter_rules(self):
        rules = []
        rules.append(('INPUT', '-s 0.0.0.0/0 -d 127.0.0.1 '
                      '-p tcp -m tcp --dport %s '
                      '-j ACCEPT' % self.conf.metadata_port))
        return rules

    def metadata_nat_rules(self):
        rules = []
        rules.append(('PREROUTING', '-s 0.0.0.0/0 -d 169.254.169.254/32 '
                     '-p tcp -m tcp --dport 80 -j REDIRECT '
                     '--to-port %s' % self.conf.metadata_port))
        return rules

    def external_gateway_nat_rules(self, ex_gw_ip, internal_cidrs,
                                   interface_name):
        rules = [('POSTROUTING', '! -i %(interface_name)s '
                  '! -o %(interface_name)s -m conntrack ! '
                  '--ctstate DNAT -j ACCEPT' % locals())]
        for cidr in internal_cidrs:
            rules.extend(self.internal_network_nat_rules(ex_gw_ip, cidr))
        return rules

    def internal_network_added(self, ri, ex_gw_port, network_id, port_id,
                               internal_cidr, mac_address):
        interface_name = self.get_internal_device_name(port_id)
        if not ip_lib.device_exists(interface_name,
                                    root_helper=self.conf.root_helper,
                                    namespace=ri.ns_name()):
            self.driver.plug(network_id, port_id, interface_name, mac_address,
                             namespace=ri.ns_name(),
                             prefix=INTERNAL_DEV_PREFIX)

        self.driver.init_l3(interface_name, [internal_cidr],
                            namespace=ri.ns_name())
        ip_address = internal_cidr.split('/')[0]
        self._send_gratuitous_arp_packet(ri, interface_name, ip_address)

        if ex_gw_port:
            ex_gw_ip = ex_gw_port['fixed_ips'][0]['ip_address']
            for c, r in self.internal_network_nat_rules(ex_gw_ip,
                                                        internal_cidr):
                ri.iptables_manager.ipv4['nat'].add_rule(c, r)
            ri.iptables_manager.apply()

    def internal_network_removed(self, ri, ex_gw_port, port_id, internal_cidr):
        interface_name = self.get_internal_device_name(port_id)
        if ip_lib.device_exists(interface_name,
                                root_helper=self.conf.root_helper,
                                namespace=ri.ns_name()):
            self.driver.unplug(interface_name, namespace=ri.ns_name(),
                               prefix=INTERNAL_DEV_PREFIX)

        if ex_gw_port:
            ex_gw_ip = ex_gw_port['fixed_ips'][0]['ip_address']
            for c, r in self.internal_network_nat_rules(ex_gw_ip,
                                                        internal_cidr):
                ri.iptables_manager.ipv4['nat'].remove_rule(c, r)
            ri.iptables_manager.apply()

    def internal_network_nat_rules(self, ex_gw_ip, internal_cidr):
        rules = [('snat', '-s %s -j SNAT --to-source %s' %
                 (internal_cidr, ex_gw_ip))]
        return rules

    def floating_ip_added(self, ri, ex_gw_port, floating_ip, fixed_ip):
        ip_cidr = str(floating_ip) + '/32'
        interface_name = self.get_external_device_name(ex_gw_port['id'])
        device = ip_lib.IPDevice(interface_name, self.conf.root_helper,
                                 namespace=ri.ns_name())

        if not ip_cidr in [addr['cidr'] for addr in device.addr.list()]:
            net = netaddr.IPNetwork(ip_cidr)
            device.addr.add(net.version, ip_cidr, str(net.broadcast))
            self._send_gratuitous_arp_packet(ri, interface_name, floating_ip)

        for chain, rule in self.floating_forward_rules(floating_ip, fixed_ip):
            ri.iptables_manager.ipv4['nat'].add_rule(chain, rule)
        ri.iptables_manager.apply()

    def floating_ip_removed(self, ri, ex_gw_port, floating_ip, fixed_ip):
        ip_cidr = str(floating_ip) + '/32'
        net = netaddr.IPNetwork(ip_cidr)
        interface_name = self.get_external_device_name(ex_gw_port['id'])

        device = ip_lib.IPDevice(interface_name, self.conf.root_helper,
                                 namespace=ri.ns_name())
        device.addr.delete(net.version, ip_cidr)

        for chain, rule in self.floating_forward_rules(floating_ip, fixed_ip):
            ri.iptables_manager.ipv4['nat'].remove_rule(chain, rule)
        ri.iptables_manager.apply()

    def floating_forward_rules(self, floating_ip, fixed_ip):
        return [('PREROUTING', '-d %s -j DNAT --to %s' %
                 (floating_ip, fixed_ip)),
                ('OUTPUT', '-d %s -j DNAT --to %s' %
                 (floating_ip, fixed_ip)),
                ('float-snat', '-s %s -j SNAT --to %s' %
                 (fixed_ip, floating_ip))]

    def router_deleted(self, context, router_id):
        """Deal with router deletion RPC message."""
        with self.sync_sem:
            if router_id in self.router_info:
                try:
                    self._router_removed(router_id)
                except Exception:
                    msg = _("Failed dealing with router "
                            "'%s' deletion RPC message")
                    LOG.debug(msg, router_id)
                    self.fullsync = True

    def routers_updated(self, context, routers):
        """Deal with routers modification and creation RPC message."""
        if not routers:
            return
        with self.sync_sem:
            try:
                self._process_routers(routers)
            except Exception:
                msg = _("Failed dealing with routers update RPC message")
                LOG.debug(msg)
                self.fullsync = True

    def _process_routers(self, routers):
        if (self.conf.external_network_bridge and
            not ip_lib.device_exists(self.conf.external_network_bridge)):
            LOG.error(_("The external network bridge '%s' does not exist")
                      % self.conf.external_network_bridge)
            return

        target_ex_net_id = self._fetch_external_net_id()

        for r in routers:
            if not r['admin_state_up']:
                continue

            # If namespaces are disabled, only process the router associated
            # with the configured agent id.
            if (not self.conf.use_namespaces and
                r['id'] != self.conf.router_id):
                continue

            ex_net_id = (r['external_gateway_info'] or {}).get('network_id')
            if not ex_net_id and not self.conf.handle_internal_only_routers:
                continue

            if ex_net_id and ex_net_id != target_ex_net_id:
                continue

            if r['id'] not in self.router_info:
                self._router_added(r['id'])

            ri = self.router_info[r['id']]
            ri.router = r
            self.process_router(ri)

    @periodic_task.periodic_task
    def _sync_routers_task(self, context):
        # we need to sync with router deletion RPC message
        with self.sync_sem:
            if self.fullsync:
                try:
                    if not self.conf.use_namespaces:
                        router_id = self.conf.router_id
                    else:
                        router_id = None
                    routers = self.plugin_rpc.get_routers(
                        context, router_id)
                    self.router_info = {}
                    self._process_routers(routers)
                    self.fullsync = False
                except Exception:
                    LOG.exception(_("Failed synchronizing routers"))
                    self.fullsync = True

    def after_start(self):
        LOG.info(_("L3 agent started"))


def main():
    eventlet.monkey_patch()
    conf = cfg.CONF
    conf.register_opts(L3NATAgent.OPTS)
    conf.register_opts(interface.OPTS)
    conf.register_opts(external_process.OPTS)
    conf()
    config.setup_logging(conf)
    server = quantum_service.Service.create(binary='quantum-l3-agent',
                                            topic=topics.L3_AGENT)
    service.launch(server).wait()
