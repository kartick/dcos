"""
Module for creating persistent SSH connections for use with synchronous
commands. Typically, tunnels should be invoked with a context manager to
ensure proper cleanup. E.G.:
with contextlib.closing(SSHTunnel(*args, **kwargs)) as tunnel:
    tunnel.write_to_remote('/usr/local/usrpath/testfile.txt', 'test_file.txt')
    tunnel.remote_cmd(['cat', 'test_file.txt'])
"""
import logging
from contextlib import closing
from subprocess import (CalledProcessError, TimeoutExpired, check_call,
                        check_output)

logger = logging.getLogger(__name__)

SOCKET_DIR = '$HOME/.cache/ssh'
SSH_CMD = [
    '/usr/bin/ssh',
    '-oConnectTimeout=10',
    '-oControlMaster=auto',
    '-oControlPath={}/%r@%h:%p'.format(SOCKET_DIR),
    '-oStrictHostKeyChecking=no',
    '-oUserKnownHostsFile=/dev/null',
    '-oBatchMode=yes',
    '-oPasswordAuthentication=no']


class SSHTunnel():

    def __init__(self, ssh_user, ssh_key_path, host, port=22):
        """Persistent SSH tunnel to avoid re-creating the same connection
        Note: this should always be instantiated with contextlib.closing
            e.g.: "with closing(SSHTunnel(*args, **kwargs)) as tunnel:"

        Args:
            ssh_user: (str) user with access to host
            ssh_key_path: (str) local path w/ permissions to ssh_user@host
            host: (str) locally resolvable hostname to tunnel to
            port: (int) port to connect to host via

        Return:
            established SSHTunnel that can be issued copy/cmd/close
        """
        self.host = host
        self.ssh_user = ssh_user
        self.ssh_key_path = ssh_key_path
        self.target = ssh_user + '@' + host
        self.port = port
        start_tunnel = SSH_CMD + [
            '-fnN',
            '-i',  ssh_key_path,
            '-p', str(port), self.target]
        start_tunnel = ' '.join(start_tunnel)
        logger.debug('Starting SSH tunnel: ' + start_tunnel)

        # Make sure we can cache the socket before starting
        check_call('mkdir -p '+SOCKET_DIR, shell=True)
        try:
            check_call(start_tunnel, shell=True)
        except CalledProcessError:
            logger.critical('SSHTunnel could not be established with cmd: ' + start_tunnel)
            raise AssertionError('Failed to create SSHTunnel with: ' + start_tunnel)

        logger.debug('SSH Tunnel established!')

    def remote_cmd(self, cmd, timeout=None):
        """
        Args:
            cmd: list of strings that will be interpretted in a subprocess
            timeout: (int) number of seconds until process timesout
        """
        assert isinstance(cmd, list), 'cmd must be a list'
        if timeout:
            assert isinstance(timeout, int), 'timeout must be an int (seconds)'
        run_cmd = SSH_CMD + ['-p', str(self.port), self.target] + cmd
        run_cmd = ' '.join(run_cmd)
        logger.debug('Running socket cmd: ' + run_cmd)
        try:
            return check_output(run_cmd, timeout=timeout, shell=True).decode('utf-8').rstrip('\r\n')
        except TimeoutExpired as e:
            logging.error('{} timed out after {} seconds'.format(cmd, timeout))
            logging.debug('Timed out process output:\n' + e.output)
            raise TimeoutExpired

    def write_to_remote(self, src, dst):
        """
        Args:
            src: (str) local path representing source data
            dst: (str) destination for path
        """
        cmd = SSH_CMD + ['-p', str(self.port), self.target, 'cat>'+dst]
        cmd = ' '.join(cmd)
        logger.debug('Running socket write: ' + cmd)
        with open(src, 'r') as fh:
            check_call(cmd, stdin=fh, shell=True)

    def close(self):
        close_tunnel = SSH_CMD + ['-p', str(self.port), '-O', 'exit', self.target]
        close_tunnel = ' '.join(close_tunnel)
        logger.debug('Closing SSH Tunnel: ' + close_tunnel)
        check_call(close_tunnel, shell=True)


class TunnelCollection():

    def __init__(self, ssh_user, ssh_key_path, host_names):
        """Convenience collection of SSHTunnels so that users can keep
        multiple connections alive with a single self-closing context
        Args:
            ssh_user: (str) user with access to host
            ssh_key_path: (str) local path w/ permissions to ssh_user@host
            host_names: list of locally resolvable hostname:port to tunnel to
        """
        assert isinstance(host_names, list)
        logger.debug('Creating TunnelCollection for the following: ' + str(host_names))
        self.tunnels = []
        for host in host_names:
            hostname, port = host.split(':')
            self.tunnels.append(SSHTunnel(ssh_user, ssh_key_path, hostname, port=port))
        logger.debug('Successfully created TunnelCollection')

    def close(self):
        for tunnel in self.tunnels:
            tunnel.close()


def run_ssh_cmd(ssh_user, ssh_key_path, host, cmd, port=22):
    """Convenience function to do a one-off SSH command
    """
    assert isinstance(cmd, list)
    with closing(SSHTunnel(ssh_user, ssh_key_path, host, port=port)) as tunnel:
        return tunnel.remote_cmd(cmd)


def run_scp_cmd(ssh_user, ssh_key_path, host, src, dst, port=22):
    """Convenience function to do a one-off SSH copy
    """
    with closing(SSHTunnel(ssh_user, ssh_key_path, host, port=port)) as tunnel:
        tunnel.write_to_remote(src, dst)
