#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Simple config builder and cloner for VirtualBox virtual machines.

See README.rst for more details.
"""
import argparse
import errno
import logging
import os
import re
import shutil
import subprocess
import sys
import threading

import yaml


def setup_logger(args):
    """Setup logger format and level"""
    level = logging.WARNING
    if args.quiet:
        level = logging.ERROR
        if args.quiet > 1:
            level = logging.CRITICAL
    if args.verbose:
        level = logging.INFO
        if args.verbose > 1:
            level = logging.DEBUG
    logging.basicConfig(level=level,
                        format="%(asctime)s %(levelname)s: %(message)s")


class Build(object):
    """Build the configuration scripts and optionally copy it to cloned VMs"""
    VM_RE = re.compile(r'^"(?P<name>.+)"\s.*')

    def __init__(self, args, config):
        self._clone = not args.dont_clone
        self._remove_vms = args.remove
        self._skip_hosts = args.skip_hosts
        self._auto_install = args.auto_install
        self.ssh_key = args.ssh_key
        self.config = config['config']
        self.context = {'controller': [], 'compute': []}
        self.hosts = config.get('nodes', {})
        self.openstack_version = config.get('openstack_version', '')
        self.base_vm = config.get('base_vm', '')
        self.base_user = config.get('base_user')
        self.base_distribution = config.get('base_distribution')
        self.base_hostname = config.get('base_hostname')
        self.modules_path = os.path.join(os.path.dirname(__file__),
                                         self.base_vm,
                                         self.openstack_version)
        if self.ssh_key:
            self.fpga_nova_repo = 'git@github.com:intelsdi-x/' \
                                  'fpga-nova.git'
        else:
            self.fpga_nova_repo = 'https://github.com/intelsdi-x/' \
                                  'fpga-nova.git'

        for hostname, data in self.hosts.items():
            self.context[data['role']].append(hostname)

    def build(self):
        """Build conf/clone vm"""
        self.validate_cloudconf()
        self.create_configs()
        self.create_cleanup()
        if self._clone:
            self.clone_vms()
        if self._auto_install:
            self.auto_install()

    def validate_cloudconf(self):
        """Make sure the specified cloud config file contains valid data"""

        if not os.path.isdir(self.modules_path):
            logging.info("Building OpenStack '%s' on '%s' is not supported."
                         " Exiting.", self.openstack_version, self.base_vm)
            sys.exit(1)

        required_vars = [self.openstack_version, self.base_vm, self.base_user,
                         self.base_distribution, self.base_hostname]
        for var in required_vars:
            if not var:
                logging.info("Cloud config file does not contain necessary "
                             "data. Fill in the config and re-run the "
                             "script. Exiting.")
                sys.exit(1)

    def _check_vms_existence(self, command="vms"):
        """Return list of existing machines, that match hosts in self.hosts"""

        result = []

        try:
            out = subprocess.check_output(['VBoxManage', 'list', command])
        except subprocess.CalledProcessError:
            return result

        for item in out.split('\n'):
            match = Build.VM_RE.match(item)
            if match and match.groups()[0] in self.hosts:
                result.append(match.groupdict()['name'])

        return result

    def _remove_vm(self, host):
        """Remove virtual machine"""
        logging.info("Removing vm `%s'.", host)
        cmd = ['VBoxManage', 'unregistervm', host]
        logging.debug("Executing: `%s'.", cmd)
        subprocess.check_call(cmd)
        cmd = ['rm', '-fr', os.path.join(os.path.expanduser('~/'), '.config',
                                         'VirtualBox', host)]
        logging.debug("Executing: `%s'.", cmd)
        subprocess.check_call(cmd)

    def _poweroff_vm(self, host):
        """Turn off virtual machine"""
        logging.info("Power off vm `%s'.", host)
        cmd = ['VBoxManage', 'controlvm', host, 'poweroff']
        logging.debug("Executing: `%s'.", cmd)
        subprocess.check_call(cmd)

    def remove_vms(self):
        """Remove vms"""
        hosts = self._check_vms_existence()

        if hosts and not self._remove_vms:
            logging.error("There is at least one VM which exists. Remove it "
                          "manually, or use --remove switch for wiping out "
                          "all existing machines before cloning."
                          "\nConflicting VMs:\n%s",
                          "\n".join(["- " + h for h in sorted(hosts)]))
            return False

        running_hosts = self._check_vms_existence('runningvms')

        for host in hosts:
            if host in running_hosts:
                self._poweroff_vm(host)
            self._remove_vm(host)

        return True

    def remap(self, line, data):
        """Replace the template placeholders to something meaningful"""
        line = line.rstrip()
        if 'CONTROLLER_HOSTNAME' in line:
            line = line.replace('CONTROLLER_HOSTNAME',
                                self.context['controller'][0])
        if 'AAA.BBB.CCC.DDD' in line:
            line = line.replace('AAA.BBB.CCC.DDD', data['ips'][0])
        if 'FPGA-NOVA-REPO' in line:
            line = line.replace('FPGA-NOVA-REPO', self.fpga_nova_repo)

        for key, val in self.config.items():
            if key in line:
                line = line.replace(key, str(val))

        return line

    def create_cleanup(self):
        """Create cleanup conf"""

        for hostname, data in self.hosts.items():
            modules_out = hostname + "_modules"

            output = ["#!/bin/bash", ""]

            for module in reversed(data['modules']):
                mod = []
                modpath = os.path.join(modules_out, "out_" + module)
                with open(os.path.join(self.modules_path,
                                       "out_" + module)) as fobj:
                    for line in fobj:
                        mod.append(self.remap(line, data))
                mod.append("")

                with open(modpath, "w") as fobj:
                    fobj.write('\n'.join(mod))

                modpath = os.path.join("/root", modpath)
                output.append("bash " + modpath)
            output.append("")

            with open(hostname + "_cleanup.sh", "w") as fobj:
                fobj.write("\n".join(output))

    def clone_vms(self):
        """Cloning VMs"""
        if not self.remove_vms():
            return

        for hostname, data in self.hosts.items():
            env = os.environ.copy()
            env.update({'VMNAME': self.base_vm,
                        'VMUSER': self.base_user,
                        'BASE_HOSTNAME': self.base_hostname,
                        'NAME': hostname,
                        'LAST_OCTET': data['ips'][0].split(".")[-1],
                        'DISTRO': self.base_distribution})
            if self.ssh_key:
                env['SSH_KEY'] = self.ssh_key

            cmd = ['./create_vm_clone.sh']
            logging.debug("Executing: %s", " ".join(cmd))
            try:
                subprocess.check_call(cmd, env=env)
            except subprocess.CalledProcessError as err:
                sys.exit(err.returncode)

    def create_configs(self):
        """Create configurations, and optionally clone and provision VMs"""

        if self._skip_hosts:
            logging.warning('Warning: You have to add appropriate entries to '
                            'your /etc/hosts, otherwise your cloud may not '
                            'work properly.')

        for hostname, data in self.hosts.items():
            modules_out = hostname + "_modules"

            try:
                os.mkdir(modules_out)
            except OSError as err:
                if err.errno == errno.EEXIST:
                    shutil.rmtree(modules_out)
                    os.mkdir(modules_out)
                else:
                    raise

            output = ["#!/bin/bash", ""]

            if not self._skip_hosts:
                for other_host_key in [x
                                       for x in self.hosts
                                       if x != hostname]:

                    output.append("echo " +
                                  self.hosts[other_host_key]['ips'][0] + " " +
                                  other_host_key +
                                  " >> /etc/hosts")
                output.append("")

            for module in data['modules']:
                mod = []
                modpath = os.path.join(modules_out, "in_" + module)
                with open(os.path.join(self.modules_path,
                                       "in_" + module)) as fobj:
                    for line in fobj:
                        mod.append(self.remap(line, data))
                mod.append("")
                with open(modpath, "w") as fobj:
                    fobj.write('\n'.join(mod))

                modpath = os.path.join("/root", modpath)
                output.append("bash " + modpath)
            output.append("")

            with open(hostname + ".sh", "w") as fobj:
                fobj.write("\n".join(output))

    def auto_install(self):
        """Triggers Openstack intallation on all nodes specified in cloud
        config file
        """
        for hostname, data in self.hosts.items():
            public_ip = data['ips'][1]
            t = threading.Thread(target=self.install_thread,
                                 args=(hostname, public_ip))
            t.start()

    def install_thread(self, hostname, public_ip):
        """Thread method that is responsible for Openstack installation
        on a single VM
        """
        logging.info("Openstack installation on host %s has started (see "
                     "%s.log)", hostname, hostname)
        env = os.environ.copy()
        env.update({'HOSTNAME': hostname,
                    'IP_ADDRESS': public_ip,
                    'VMUSER': self.base_user})
        logging.debug("Executing in thread: " + './boot_vm_and_install.sh')
        subprocess.check_call(['./boot_vm_and_install.sh'], env=env)
        logging.info("Openstack installation on host %s has finished.",
                     hostname)


def main():
    """Main function, just parses arguments, and call create_configs"""
    parser = argparse.ArgumentParser()
    parser.add_argument('--dont-clone', '-d', action='store_true',
                        help='Do not clone machine, just generate install '
                        'scripts')
    parser.add_argument('--skip-hosts', '-s', action='store_true',
                        help='Skip appending hosts to /etc/hosts')
    parser.add_argument('--remove', '-r', action='store_true',
                        help='Dispose existing VMs')
    parser.add_argument('--auto-install', '-a', action='store_true',
                        help='Automatically start VMs and run Openstack '
                             'installation')
    parser.add_argument('--ssh-key', '-k', help='Path to private SSH key used'
                                                ' to clone git repositories')
    parser.add_argument('cloudconf',
                        help='Yaml file with the cloud configuration')
    parser.add_argument('-v', '--verbose', help='Be verbose. Adding more "v" '
                        'will increase verbosity', action="count",
                        default=None)
    parser.add_argument('-q', '--quiet', help='Be quiet. Adding more "q" will'
                        ' decrease verbosity', action="count", default=None)
    parsed_args = parser.parse_args()

    with open(parsed_args.cloudconf) as fobj:
        conf = yaml.load(fobj)

    setup_logger(parsed_args)
    Build(parsed_args, conf).build()


if __name__ == "__main__":
    main()
