#!/usr/bin/env python3
"""Module for running integration_test.py inside of a remote cluster
Parameters for integration_test.py are passed via the same environment variables

Usage:
    test_runner [options] setup <ssh_user> <ssh_key_path> <test_dir> <test_host>
    test_runner [options] <ssh_user> <ssh_key_path> <test_dir> <test_host>
    test_runner -h | --help

Options:
    -h --help
    --log_level=<log_lvl>   One of: debug, info, wanring, error, critical [default: info]
    --port=<port>           Port that test_host is accessible by [default: 22]
    --local_ip=<local_ip>   The local IP of the test host. Only set if test_host is NOT apart of target DCOS
    --timeout=<timeout>     String to pass as arg to /usr/bin/timeout wrapper for integration_test
"""
import json
import logging
import os
import time
from contextlib import closing
from subprocess import CalledProcessError

import pkg_resources
from docopt import docopt
from retrying import retry

from ssh import SSHTunnel

logger = logging.getLogger(__name__)

TEST_DOCKERD_CONFIG = """[Service]
Restart=always
StartLimitInterval=0
RestartSec=15
ExecStart=
ExecStart=/usr/bin/docker daemon -H fd:// --storage-driver=overlay --insecure-registry={}:5000
"""


def pkg_filename(relative_path):
    return pkg_resources.resource_filename(__name__, relative_path)


def get_agent_list_from_mesos_master(tunnel, mesos_master):
    """SSH into public master and query mesos/state-summary"""
    assert isinstance(tunnel, SSHTunnel)
    logger.info('Querying mesos master for state-summary...')
    logger.warning('master state-summary will only provide nodes that '
                    'successfully registered! integration_test will be biased')
    state_summary = tunnel.remote_cmd(['curl', "http://{}:5050/state-summary".format(mesos_master)])
    agent_list = json.loads(state_summary)['slaves']
    output = []
    for agent in agent_list:
        output.append(agent['hostname'])
    return output


def setup_integration_test(tunnel, test_dir, registry=None, agent_list=None):
    """Transfer resources and issues commands on host to build test app,
    host it on a docker registry, and prepare the integration_test container

    Args:
        registry (str): address of registry host that is visible to test nodes (DCOS local IP of test_host)
        test_dir (str): path to be used for setup and file transfer on host

    Returns:
        result from async chain that can be checked later for success
    """
    test_server_docker = pkg_filename('docker/test_server/Dockerfile')
    test_server_script = pkg_filename('docker/test_server/test_server.py')
    pytest_docker = pkg_filename('docker/py.test/Dockerfile')
    logger.info('Setting up integration_test.py to run on ' + tunnel.host)
    tunnel.remote_cmd(['mkdir', '-p', test_dir])

    if not registry:
        logger.warning('No registry provided; using test host as registry')
        logger.warning('Assuming that test host is a node in DCOS')
        logger.info('Finding IP local of test host')
        registry = tunnel.remote_cmd(['/opt/mesosphere/bin/detect_ip'])

    if not agent_list:
        logger.warning('No agent_list provided; will be provided by mesos master')
        agent_list = get_agent_list_from_mesos_master(tunnel, registry)

    def remote(path):
        return test_dir + '/' + path

    logger.info('Setting up SSH key on test host for daisy-chain-ing')
    remote_key_path = remote('test_ssh_key')
    tunnel.write_to_remote(tunnel.ssh_key_path, remote_key_path)
    tunnel.remote_cmd(['chmod', '600', remote_key_path])

    logger.info('Reconfiguring all dockerd to trust insecurity registry: ' + registry)
    with open('execstart.conf', 'w') as conf_fh:
        conf_fh.write(TEST_DOCKERD_CONFIG.format(registry))
    conf_transfer_path = remote('execstart.conf')
    docker_conf_chain = (
        ['sudo', 'cp', conf_transfer_path, '/etc/systemd/system/docker.service.d/execstart.conf'],
        ['sudo', 'systemctl', 'daemon-reload'],
        ['sudo', 'systemctl', 'restart', 'docker'])
    logger.info('Reconfiguring dockerd on test host')
    tunnel.write_to_remote('execstart.conf', conf_transfer_path)
    for cmd in docker_conf_chain:
        tunnel.remote_cmd(cmd)
    for agent in agent_list:
        logger.info('Reconfiguring dockerd on ' + agent)
        target = "{}@{}".format(tunnel.ssh_user, agent)
        target_scp = "{}:{}".format(target, conf_transfer_path)
        remote_scp = ['/usr/bin/scp'] + SSH_OPTS + ['-i', remote_key_path, conf_transfer_path, target_scp]
        tunnel.remote_cmd(remote_scp)
        chain_prefix = ['/usr/bin/ssh'] + SSH_OPTS + ['-tt', '-i', remote_key_path, target]
        for cmd in docker_conf_chain:
            tunnel.remote_cmd(chain_prefix+cmd)

    tunnel.remote_cmd(['mkdir', '-p', remote('test_server')])
    tunnel.write_to_remote(test_server_docker, remote('test_server/Dockerfile'))
    tunnel.write_to_remote(test_server_script, remote('test_server/test_server.py'))
    logger.info('Starting insecure registry on test host')
    try:
        logger.debug('Attempt to replace a previously setup registry')
        tunnel.remote_cmd(['sudo', 'docker', 'kill', 'registry'])
        tunnel.remote_cmd(['sudo', 'docker', 'rm', 'registry'])
    except CalledProcessError:
        logger.debug('No previous registry to kill or delete')
    tunnel.remote_cmd([
        'sudo', 'docker', 'run', '-d', '-p', '5000:5000', '--restart=always', '--name',
        'registry', 'registry:2'])
    logger.info('Building test_server Docker image on test host')
    tunnel.remote_cmd([
        'cd', remote('test_server'), '&&', 'sudo', 'docker', 'build', '-t',
        '{}:5000/test_server'.format(registry), '.'])
    logger.info('Pushing built test server to insecure registry')
    tunnel.remote_cmd(['sudo', 'docker', 'push', "{}:5000/test_server".format(registry)])
    logger.debug('Cleaning up test_server files')
    tunnel.remote_cmd(['rm', '-rf', remote('test_server')])
    tunnel.remote_cmd(['mkdir', '-p', remote('py.test')])
    logger.info('Setting up integration_test.py container on test host')
    tunnel.write_to_remote(pytest_docker, remote('py.test/Dockerfile'))
    tunnel.remote_cmd([
        'cd', remote('py.test'), '&&', 'sudo', 'docker', 'build', '-t', 'py.test', '.'])
    tunnel.remote_cmd(['rm', '-rf', remote('py.test')])


def integration_test(
        tunnel, test_dir,
        dcos_dns, master_list, agent_list, registry_host,
        variant, test_dns_search, ci_flags, timeout=None,
        aws_access_key_id='', aws_secret_access_key='', region=''):
    """Runs integration test on host

    Args:
        test_dir: string representing host where integration_test.py exists on test_host
        dcos_dns: string representing IP of DCOS DNS host
        master_list: string of comma separated master addresses
        agent_list: string of comma separated agent addresses
        registry_host: string for address where marathon can pull test app
        variant: 'ee' or 'default'
        test_dns_search: if set to True, test for deployed mesos DNS app
        ci_flags: optional additional string to be passed to test
        # The following variables correspond to currently disabled tests
        aws_access_key_id: needed for REXRAY tests
        aws_secret_access_key: needed for REXRAY tests
        region: string indicating AWS region in which cluster is running
    """
    assert not test_dir.endswith('/')

    logger.info('Transfering integration_test.py')
    test_script = pkg_filename('integration_test.py')
    tunnel.remote_cmd(['mkdir', '-p', test_dir])
    tunnel.write_to_remote(test_script, test_dir+'/integration_test.py')

    test_container_name = 'int_test_' + str(int(time.time()))
    dns_search = 'true' if test_dns_search else 'false'
    test_cmd = [
        'sudo', 'docker', 'run', '-v', test_dir+'/integration_test.py:/integration_test.py',
        '-e', 'DCOS_DNS_ADDRESS=http://'+dcos_dns,
        '-e', 'MASTER_HOSTS='+','.join(master_list),
        '-e', 'PUBLIC_MASTER_HOSTS='+','.join(master_list),
        '-e', 'SLAVE_HOSTS='+','.join(agent_list),
        '-e', 'REGISTRY_HOST='+registry_host,
        '-e', 'DCOS_VARIANT='+variant,
        '-e', 'DNS_SEARCH='+dns_search,
        '-e', 'AWS_ACCESS_KEY_ID='+aws_access_key_id,
        '-e', 'AWS_SECRET_ACCESS_KEY='+aws_secret_access_key,
        '-e', 'AWS_REGION='+region,
        '--net=host', '--name='+test_container_name, 'py.test', 'py.test',
        '-vv', ci_flags, '/integration_test.py']
    try:
        test_output = tunnel.remote_cmd(test_cmd, timeout=timeout)
        logger.info('Successful test run! Log output:\n'+test_output)
    except CalledProcessError as e:
        if e.returncode == 124 and timeout:
            logger.error('Test timed out after {} seconds'.format(timeout))
        else:
            logger.error('Test failed, see output below:\n'+e.output.decode('utf-8'))
        get_logs_cmd = ['docker', 'logs', test_container_name]
        logger.info('Failed test log:\n'+tunnel.remote_cmd(get_logs_cmd))
        raise AssertionError('Test failed. See log output above')


def run_basic_integration_test(tunnel, local_ip, test_dir, timeout=None):
    """This will assume a single master configuration that will be used to run thest
    and then derive the agent list from a mesos state summary
    Note: environment variables still take precedence for overide
    """
    if not local_ip:
        logger.warning('local_ip not provided, assuming test_host is a DCOS node')
        logger.info('Finding local IP of test_host')
        local_ip = tunnel.remote_cmd(['/opt/mesosphere/bin/detect_ip'])
    agent_list = os.environ.get('SLAVE_HOSTS', '')
    master_list = os.environ.get('MASTER_HOSTS', '')
    dcos_dns = os.environ.get('DCOS_DNS_ADDRESS', '')
    if agent_list == '':
        logger.warning('SLAVE_HOSTS unset in env. Getting list from mesos master')
        agent_list = get_agent_list_from_mesos_master(tunnel, local_ip)
    if master_list == '':
        logger.warning('MASTER_HOSTS unset in env. Assuming test_host is master')
        master_list = [local_ip]
    if dcos_dns == '':
        dcos_dns = master_list[0]
    integration_test(
        tunnel=tunnel,
        test_dir=test_dir,
        dcos_dns=dcos_dns,
        master_list=master_list,
        agent_list=agent_list,
        registry_host=os.environ.get('REGISTRY_HOST', local_ip),
        variant=os.environ.get('DCOS_VARIANT', 'default'),
        test_dns_search=os.environ.get('DNS_SEARCH', 'false') == 'true',
        ci_flags=os.environ.get('CI_FLAGS', ''),
        aws_access_key_id=os.environ.get('AWS_ACCESS_KEY_ID', ''),
        aws_secret_access_key=os.environ.get('AWS_SECRET_ACCESS_KEY', ''),
        region=os.environ.get('AWS_REGION', ''),
        timeout=timeout)


def main():
    arguments = docopt(__doc__, version='0.0.1')
    logger.setLevel(getattr(logging, arguments['--log_level'].upper()))
    tunnel_args = {
            'ssh_user': arguments['<ssh_user>'],
            'ssh_key_path': arguments['<ssh_key_path>'],
            'host': arguments['<test_host>'],
            'port': arguments['--port']}
    with closing(SSHTunnel(**tunnel_args)) as tunnel:
        if arguments['setup']:
            setup_integration_test(
                    tunnel,
                    arguments['<test_dir>'],
                    registry=arguments['--local_ip'])
        else:
            run_basic_integration_test(
                    tunnel,
                    arguments['--local_ip'],
                    arguments['<test_dir>'],
                    arguments['--timeout'])


if __name__ == '__main__':
    main()
