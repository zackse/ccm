# ccm node
from __future__ import with_statement

import common, yaml, os, errno, signal, time, subprocess, shutil, sys, glob, re, stat
import repository
from cli_session import CliSession

class Status():
    UNINITIALIZED = "UNINITIALIZED"
    UP = "UP"
    DOWN = "DOWN"
    DECOMMISIONNED = "DECOMMISIONNED"

class NodeError(Exception):
    def __init__(self, msg, process=None):
        Exception.__init__(self, msg)
        self.process = process

class TimeoutError(Exception):
    def __init__(self, data):
        Exception.__init__(self, str(data))

# Groups: 1 = cf, 2 = tmp or none, 3 = suffix (Compacted or Data.db)
_sstable_regexp = re.compile('(?P<cf>[\S]+)+-(?P<tmp>tmp-)?[\S]+-(?P<suffix>[a-zA-Z.]+)')

class Node():
    """
    Provides interactions to a Cassandra node.
    """

    def __init__(self, name, cluster, auto_bootstrap, thrift_interface, storage_interface, jmx_port, remote_debug_port, initial_token, save=True, binary_interface=None):
        """
        Create a new Node.
          - name: the name for that node
          - cluster: the cluster this node is part of
          - auto_boostrap: whether or not this node should be set for auto-boostrap
          - thrift_interface: the (host, port) tuple for thrift
          - storage_interface: the (host, port) tuple for internal cluster communication
          - jmx_port: the port for JMX to bind to
          - remote_debug_port: the port for remote debugging
          - initial_token: the token for this node. If None, use Cassandra token auto-assigment
          - save: copy all data useful for this node to the right position.  Leaving this true
            is almost always the right choice.
        """
        self.name = name
        self.cluster = cluster
        self.status = Status.UNINITIALIZED
        self.auto_bootstrap = auto_bootstrap
        self.network_interfaces = { 'thrift' : thrift_interface, 'storage' : storage_interface, 'binary' : binary_interface }
        self.jmx_port = jmx_port
        self.remote_debug_port = remote_debug_port
        self.initial_token = initial_token
        self.pid = None
        self.data_center = None
        self.__config_options = {}
        self.__cassandra_dir = None
        self.__global_log_level = None
        self.__classes_log_level = {}
        if save:
            self.import_config_files()
            self.import_bin_files()

    @staticmethod
    def load(path, name, cluster):
        """
        Load a node from from the path on disk to the config files, the node name and the
        cluster the node is part of.
        """
        node_path = os.path.join(path, name)
        filename = os.path.join(node_path, 'node.conf')
        with open(filename, 'r') as f:
            data = yaml.load(f)
        try:
            itf = data['interfaces']
            initial_token = None
            if 'initial_token' in data:
                initial_token = data['initial_token']
            remote_debug_port = 2000
            if 'remote_debug_port' in data:
                remote_debug_port = data['remote_debug_port']
            binary_interface = None
            if 'binary' in itf and itf['binary'] is not None:
                binary_interface = tuple(itf['binary'])
            node = Node(data['name'], cluster, data['auto_bootstrap'], tuple(itf['thrift']), tuple(itf['storage']), data['jmx_port'], remote_debug_port, initial_token, save=False, binary_interface=binary_interface)
            node.status = data['status']
            if 'pid' in data:
                node.pid = int(data['pid'])
            if 'cassandra_dir' in data:
                node.__cassandra_dir = data['cassandra_dir']
            if 'config_options' in data:
                node.__config_options = data['config_options']
            if 'data_center' in data:
                node.data_center = data['data_center']
            return node
        except KeyError as k:
            raise common.LoadError("Error Loading " + filename + ", missing property: " + str(k))

    def get_path(self):
        """
        Returns the path to this node top level directory (where config/data is stored)
        """
        return os.path.join(self.cluster.get_path(), self.name)

    def get_bin_dir(self):
        """
        Returns the path to the directory where Cassandra scripts are located
        """
        return os.path.join(self.get_path(), 'bin')

    def get_conf_dir(self):
        """
        Returns the path to the directory where Cassandra config are located
        """
        return os.path.join(self.get_path(), 'conf')

    def address(self):
        """
        Returns the IP use by this node for internal communication
        """
        return self.network_interfaces['storage'][0]

    def get_cassandra_dir(self):
        """
        Returns the path to the cassandra source directory used by this node.
        """
        if self.__cassandra_dir is None:
            return self.cluster.get_cassandra_dir()
        else:
            common.validate_cassandra_dir(self.__cassandra_dir)
            return self.__cassandra_dir

    def set_cassandra_dir(self, cassandra_dir=None, cassandra_version=None, verbose=False):
        """
        Sets the path to the cassandra source directory for use by this node.
        """
        if cassandra_version is None:
            self.__cassandra_dir = cassandra_dir
            if cassandra_dir is not None:
                common.validate_cassandra_dir(cassandra_dir)
        else:
            dir, v = repository.setup(cassandra_version, verbose=verbose)
            self.__cassandra_dir = dir
        self.import_config_files()
        return self

    def set_configuration_options(self, values=None, batch_commitlog=None):
        """
        Set Cassandra configuration options.
        ex:
            node.set_configuration_options(values={
                'hinted_handoff_enabled' : True,
                'concurrent_writes' : 64,
            })
        The batch_commitlog option gives an easier way to switch to batch
        commitlog (since it requires setting 2 options and unsetting one).
        """
        if values is not None:
            for k, v in values.iteritems():
                self.__config_options[k] = v
        if batch_commitlog is not None:
            if batch_commitlog:
                self.__config_options["commitlog_sync"] = "batch"
                self.__config_options["commitlog_sync_batch_window_in_ms"] = 5
                self.__config_options["commitlog_sync_period_in_ms"] = None
            else:
                self.__config_options["commitlog_sync"] = "periodic"
                self.__config_options["commitlog_sync_period_in_ms"] = 10000
                self.__config_options["commitlog_sync_batch_window_in_ms"] = None

        self.import_config_files()

    def show(self, only_status=False, show_cluster=True):
        """
        Print infos on this node configuration.
        """
        self.__update_status()
        indent = ''.join([ " " for i in xrange(0, len(self.name) + 2) ])
        print "%s: %s" % (self.name, self.__get_status_string())
        if not only_status:
            if show_cluster:
                print "%s%s=%s" % (indent, 'cluster', self.cluster.name)
            print "%s%s=%s" % (indent, 'auto_bootstrap', self.auto_bootstrap)
            print "%s%s=%s" % (indent, 'thrift', self.network_interfaces['thrift'])
            if self.network_interfaces['binary'] is not None:
                print "%s%s=%s" % (indent, 'binary', self.network_interfaces['binary'])
            print "%s%s=%s" % (indent, 'storage', self.network_interfaces['storage'])
            print "%s%s=%s" % (indent, 'jmx_port', self.jmx_port)
            print "%s%s=%s" % (indent, 'remote_debug_port', self.remote_debug_port)
            print "%s%s=%s" % (indent, 'initial_token', self.initial_token)
            if self.pid:
                print "%s%s=%s" % (indent, 'pid', self.pid)

    def is_running(self):
        """
        Return true if the node is running
        """
        self.__update_status()
        return self.status == Status.UP or self.status == Status.DECOMMISIONNED

    def is_live(self):
        """
        Return true if the node is live (it's run and is not decommissionned).
        """
        self.__update_status()
        return self.status == Status.UP

    def logfilename(self):
        """
        Return the path to the current Cassandra log of this node.
        """
        return os.path.join(self.get_path(), 'logs', 'system.log')

    def grep_log(self, expr):
        """
        Returns a list of lines matching the regular expression in parameter
        in the Cassandra log of this node
        """
        matchings = []
        pattern = re.compile(expr)
        with open(self.logfilename()) as f:
            for line in f:
                m = pattern.search(line)
                if m:
                    matchings.append((line, m))
        return matchings

    def mark_log(self):
        """
        Returns "a mark" to the current position of this node Cassandra log.
        This is for use with the from_mark parameter of watch_log_for_* methods,
        allowing to watch the log from the position when this method was called.
        """
        if not os.path.exists(self.logfilename()):
            return 0
        with open(self.logfilename()) as f:
            f.seek(0, os.SEEK_END)
            return f.tell()

    def print_process_output(self, name, proc, verbose=False):
        if verbose:
            for line in proc.stdout:
                print "[%s] %s" % (name, line.rstrip('\n'))
        for line in proc.stderr:
            print "[%s ERROR] %s" % (name, line.rstrip('\n'))


    # This will return when exprs are found or it timeouts
    def watch_log_for(self, exprs, from_mark=None, timeout=600, process=None, verbose=False):
        """
        Watch the log until one or more (regular) expression are found.
        This methods when all the expressions have been found or the method
        timeouts (a TimeoutError is then raised). On successful completion,
        a list of pair (line matched, match object) is returned.
        """
        elapsed = 0
        tofind = [exprs] if isinstance(exprs, basestring) else exprs
        tofind = [ re.compile(e) for e in tofind ]
        matchings = []
        reads = ""
        if len(tofind) == 0:
            return None

        while not os.path.exists(self.logfilename()):
            time.sleep(.5)
            if process:
                process.poll()
                if process.returncode is not None:
                    self.print_process_output(self.name, process, verbose)
                    if process.returncode != 0:
                        raise RuntimeError() # Shouldn't reuse RuntimeError but I'm lazy

        with open(self.logfilename()) as f:
            if from_mark:
                f.seek(from_mark)

            while True:
                # First, if we have a process to check, then check it
                if process:
                    process.poll()
                    if process.returncode is not None:
                        self.print_process_output(self.name, process, verbose)
                        if process.returncode != 0:
                            raise RuntimeError() # Shouldn't reuse RuntimeError but I'm lazy

                line = f.readline()
                if line:
                    reads = reads + line
                    for e in tofind:
                        m = e.search(line)
                        if m:
                            matchings.append((line, m))
                            tofind.remove(e)
                            if len(tofind) == 0:
                                return matchings[0] if isinstance(exprs, basestring) else matchings
                else:
                    # yep, it's ugly
                    time.sleep(1)
                    elapsed = elapsed + 1
                    if elapsed > timeout:
                        raise TimeoutError(time.strftime("%d %b %Y %H:%M:%S", time.gmtime()) + " [" + self.name + "] Missing: " + str([e.pattern for e in tofind]) + ":\n" + reads)

                if process:
                    process.poll()
                    if process.returncode is not None and process.returncode == 0:
                        return None

    def watch_log_for_death(self, nodes, from_mark=None, timeout=600):
        """
        Watch the log of this node until it detects that the provided other
        nodes are marked dead. This method returns nothing but throw a
        TimeoutError if all the requested node have not been found to be
        marked dead before timeout sec.
        A mark as returned by mark_log() can be used as the from_mark
        parameter to start watching the log from a given position. Otherwise
        the log is watched from the beginning.
        """
        tofind = nodes if isinstance(nodes, list) else [nodes]
        tofind = [ "%s is now [dead|DOWN]" % node.address() for node in tofind ]
        self.watch_log_for(tofind, from_mark=from_mark, timeout=timeout)

    def watch_log_for_alive(self, nodes, from_mark=None, timeout=120):
        """
        Watch the log of this node until it detects that the provided other
        nodes are marked UP. This method works similarily to watch_log_for_death.
        """
        tofind = nodes if isinstance(nodes, list) else [nodes]
        tofind = [ "%s.* now UP" % node.address() for node in tofind ]
        self.watch_log_for(tofind, from_mark=from_mark, timeout=timeout)

    def start(self,
              join_ring=True,
              no_wait=False,
              verbose=False,
              update_pid=True,
              wait_other_notice=False,
              replace_token=None,
              replace_address=None,
              jvm_args=[],
              wait_for_binary_proto=False,
              profile_options=None):
        """
        Start the node. Options includes:
          - join_ring: if false, start the node with -Dcassandra.join_ring=False
          - no_wait: by default, this method returns when the node is started and listening to clients.
            If no_wait=True, the method returns sooner.
          - wait_other_notice: if True, this method returns only when all other live node of the cluster
            have marked this node UP.
          - replace_token: start the node with the -Dcassandra.replace_token option.
          - replace_address: start the node with the -Dcassandra.replace_address option.
        """
        if self.is_running():
            raise NodeError("%s is already running" % self.name)

        for itf in self.network_interfaces.values():
            if itf is not None:
                common.check_socket_available(itf)

        if wait_other_notice:
            marks = [ (node, node.mark_log()) for node in self.cluster.nodes.values() if node.is_running() ]

        cdir = self.get_cassandra_dir()
        cass_bin = os.path.join(cdir, 'bin', 'cassandra')

        # Copy back the cassandra script since profiling may have modified it the previous time
        shutil.copy(cass_bin, self.get_bin_dir())
        cass_bin = os.path.join(self.get_bin_dir(), 'cassandra')

        if profile_options is not None:
            config = common.get_config()
            if not 'yourkit_agent' in config:
                raise NodeError("Cannot enable profile. You need to set 'yourkit_agent' to the path of your agent in a ~/.ccm/config")
            cmd = '-agentpath:%s' % config['yourkit_agent']
            if 'options' in profile_options:
                cmd = cmd + '=' + profile_options['options']
            print cmd
            # Yes, it's fragile as shit
            pattern=r'cassandra_parms="-Dlog4j.configuration=log4j-server.properties -Dlog4j.defaultInitOverride=true'
            common.replace_in_file(cass_bin, pattern, '    ' + pattern + ' ' + cmd + '"')

        os.chmod(cass_bin, os.stat(cass_bin).st_mode | stat.S_IEXEC)

        env = common.make_cassandra_env(cdir, self.get_path())
        pidfile = os.path.join(self.get_path(), 'cassandra.pid')
        args = [ cass_bin, '-p', pidfile, '-Dcassandra.join_ring=%s' % str(join_ring) ]
        if replace_token is not None:
            args.append('-Dcassandra.replace_token=%s' % str(replace_token))
        if replace_address is not None:
            args.append('-Dcassandra.replace_address=%s' % str(replace_address))
        args = args + jvm_args

        process = subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        if update_pid:
            if no_wait:
                time.sleep(2) # waiting 2 seconds nevertheless to check for early errors and for the pid to be set
            else:
                for line in process.stdout:
                    if verbose:
                        print line.rstrip('\n')

            self._update_pid(process)

            if not self.is_running():
                raise NodeError("Error starting node %s" % self.name, process)

        if wait_other_notice:
            for node, mark in marks:
                node.watch_log_for_alive(self, from_mark=mark)

        if wait_for_binary_proto:
            self.watch_log_for("Starting listening for CQL clients")
            # we're probably fine at that point but just wait some tiny bit more because
            # the msg is logged just before starting the binary protocol server
            time.sleep(0.2)

        return process

    def stop(self, wait=True, wait_other_notice=False, gently=True):
        """
        Stop the node.
          - wait: if True (the default), wait for the Cassandra process to be
            really dead. Otherwise return after having sent the kill signal.
          - wait_other_notice: return only when the other live nodes of the
            cluster have marked this node has dead.
          - gently: Let Cassandra clean up and shut down properly. Otherwise do
            a 'kill -9' which shuts down faster.
        """
        if self.is_running():
            if wait_other_notice:
                #tstamp = time.time()
                marks = [ (node, node.mark_log()) for node in self.cluster.nodes.values() if node.is_running() and node is not self ]

            if gently:
                os.kill(self.pid, signal.SIGTERM)
            else:
                os.kill(self.pid, signal.SIGKILL)

            if wait_other_notice:
                for node, mark in marks:
                    node.watch_log_for_death(self, from_mark=mark)
                    #print node.name, "has marked", self.name, "down in " + str(time.time() - tstamp) + "s"
            else:
                time.sleep(.1)

            still_running = self.is_running()
            if still_running and wait:
                wait_time_sec = 1
                for i in xrange(0, 7):
                    # we'll double the wait time each try and cassandra should
                    # not take more than 1 minute to shutdown
                    time.sleep(wait_time_sec)
                    if not self.is_running():
                        return True
                    wait_time_sec = wait_time_sec * 2
                raise NodeError("Problem stopping node %s" % self.name)
            else:
                return True
        else:
            return False

    def nodetool(self, cmd):
        cdir = self.get_cassandra_dir()
        nodetool = os.path.join(cdir, 'bin', 'nodetool')
        env = common.make_cassandra_env(cdir, self.get_path())
        host = self.address()
        args = [ nodetool, '-h', host, '-p', str(self.jmx_port)]
        args += cmd.split()
        p = subprocess.Popen(args, env=env)
        p.wait()

    def scrub(self, options):
        cdir = self.get_cassandra_dir()
        scrub_bin = os.path.join(cdir, 'bin', 'sstablescrub')
        env = common.make_cassandra_env(cdir, self.get_path())
        os.execve(scrub_bin, [ 'sstablescrub' ] + options, env)

    def run_cli(self, cmds=None, show_output=False, cli_options=[]):
        cdir = self.get_cassandra_dir()
        cli = os.path.join(cdir, 'bin', 'cassandra-cli')
        env = common.make_cassandra_env(cdir, self.get_path())
        host = self.network_interfaces['thrift'][0]
        port = self.network_interfaces['thrift'][1]
        args = [ '-h', host, '-p', str(port) , '--jmxport', str(self.jmx_port) ] + cli_options
        sys.stdout.flush()
        if cmds is None:
            os.execve(cli, [ 'cassandra-cli' ] + args, env)
        else:
            p = subprocess.Popen([ cli ] + args, env=env, stdin=subprocess.PIPE, stderr=subprocess.PIPE, stdout=subprocess.PIPE)
            for cmd in cmds.split(';'):
                p.stdin.write(cmd + ';\n')
            p.stdin.write("quit;\n")
            p.wait()
            for err in p.stderr:
                print "(EE) " + err,
            if show_output:
                i = 0
                for log in p.stdout:
                    # first four lines are not interesting
                    if i >= 4:
                        print log,
                    i = i + 1

    def run_cqlsh(self, cmds=None, show_output=False, cqlsh_options=[]):
        cdir = self.get_cassandra_dir()
        cli = os.path.join(cdir, 'bin', 'cqlsh')
        env = common.make_cassandra_env(cdir, self.get_path())
        host = self.network_interfaces['thrift'][0]
        port = self.network_interfaces['thrift'][1]
        args = cqlsh_options + [ host, str(port) ]
        sys.stdout.flush()
        if cmds is None:
            os.execve(cli, [ 'cqlsh' ] + args, env)
        else:
            p = subprocess.Popen([ cli ] + args, env=env, stdin=subprocess.PIPE, stderr=subprocess.PIPE, stdout=subprocess.PIPE)
            for cmd in cmds.split(';'):
                p.stdin.write(cmd + ';\n')
            p.stdin.write("quit;\n")
            p.wait()
            for err in p.stderr:
                print "(EE) " + err,
            if show_output:
                i = 0
                for log in p.stdout:
                    # first four lines are not interesting
                    if i >= 4:
                        print log,
                    i = i + 1

    def cli(self):
        cdir = self.get_cassandra_dir()
        cli = os.path.join(cdir, 'bin', 'cassandra-cli')
        env = common.make_cassandra_env(cdir, self.get_path())
        host = self.network_interfaces['thrift'][0]
        port = self.network_interfaces['thrift'][1]
        args = [ '-h', host, '-p', str(port) , '--jmxport', str(self.jmx_port) ]
        return CliSession(subprocess.Popen([ cli ] + args, env=env, stdin=subprocess.PIPE, stderr=subprocess.PIPE, stdout=subprocess.PIPE))

    def set_log_level(self, new_level, class_name=None):
        known_level = [ 'TRACE', 'DEBUG', 'INFO', 'WARN', 'ERROR' ]
        if new_level not in known_level:
            raise common.ArgumentError("Unknown log level %s (use one of %s)" % (new_level, " ".join(known_level)))

        if class_name:
            self.__classes_log_level[class_name] = new_level
        else:
            self.__global_log_level = new_level
        version = self.cluster.version()
        #loggers changed > 2.1
        if float(version[:version.index('.')+2]) < 2.1:
            self.__update_log4j()
        else:
            self.__update_logback()
        return self

    #
    # Update log4j config: copy new log4j-server.properties into 
    # ~/.ccm/name-of-cluster/nodeX/conf/log4j-server.properties
    #
    def update_log4j(self, new_log4j_config):
        cassandra_conf_dir = os.path.join(self.get_conf_dir(), 
                                           'log4j-server.properties')
        common.copy_file(new_log4j_config, cassandra_conf_dir)        

    #
    # Update logback config: copy new logback.xml into
    # ~/.ccm/name-of-cluster/nodeX/conf/logback.xml
    #
    def update_logback(self, new_logback_config):
        cassandra_conf_dir = os.path.join(self.get_conf_dir(),
                                           'logback.xml')
        common.copy_file(new_logback_config, cassandra_conf_dir)

    def clear(self, clear_all = False, only_data = False):
        data_dirs = [ 'data' ]
        if not only_data:
            data_dirs = data_dirs + [ 'commitlogs']
            if clear_all:
                data_dirs = data_dirs + [ 'saved_caches', 'logs']
        for d in data_dirs:
            full_dir = os.path.join(self.get_path(), d)
            if only_data:
                for dir in os.listdir(full_dir):
                    keyspace_dir = os.path.join(full_dir, dir)
                    if os.path.isdir(keyspace_dir) and dir != "system":
                        for f in os.listdir(keyspace_dir):
                            full_path = os.path.join(keyspace_dir, f)
                            if os.path.isfile(full_path):
                                os.remove(full_path)
            else:
                shutil.rmtree(full_dir)
                os.mkdir(full_dir)

    def run_sstable2json(self, keyspace=None, datafile=None, column_families=None, enumerate_keys=False):
        cdir = self.get_cassandra_dir()
        sstable2json = os.path.join(cdir, 'bin', 'sstable2json')
        env = common.make_cassandra_env(cdir, self.get_path())
        datafiles = self.__gather_sstables(datafile,keyspace,column_families)

        for file in datafiles:
            print "-- {0} -----".format(os.path.basename(file))
            args = [ sstable2json , file ]
            if enumerate_keys:
                args = args + ["-e"]
            subprocess.call(args, env=env)
            print ""

    def run_sstablesplit(self, datafile=None,  size=None, keyspace=None, column_families=None):
        cdir = self.get_cassandra_dir()
        sstablesplit = os.path.join(cdir, 'bin', 'sstablesplit')
        env = common.make_cassandra_env(cdir, self.get_path())
        datafiles = self.__gather_sstables(datafile, keyspace, column_families)

        def do_split(f):
            print "-- {0}-----".format(os.path.basename(f))
            if size is not None:
                subprocess.call( [sstablesplit, '-s', str(size), f], env=env )
            else:
                subprocess.call( [sstablesplit, f], env=env )

        for datafile in datafiles:
            do_split(datafile)

    def list_keyspaces(self):
        keyspaces = os.listdir(os.path.join(self.get_path(), 'data'))
        keyspaces.remove('system')
        return keyspaces

    def get_sstables(self, keyspace, column_family):
        keyspace_dir = os.path.join(self.get_path(), 'data', keyspace)
        if not os.path.exists(keyspace_dir):
            raise common.ArgumentError("Unknown keyspace {0}".format(keyspace))

        version = self.cluster.version()
        # data directory layout is changed from 1.1
        if float(version[:version.index('.')+2]) < 1.1:
            files = glob.glob(os.path.join(keyspace_dir, "{0}*-Data.db".format(column_family)))
        else:
            files = glob.glob(os.path.join(keyspace_dir, column_family or "*", "%s-%s*-Data.db" % (keyspace, column_family)))
        for f in files:
            if os.path.exists(f.replace('Data.db', 'Compacted')):
                files.remove(f)
        return files

    def stress(self, stress_options):
        stress = common.get_stress_bin(self.get_cassandra_dir())
        args = [ stress ] + stress_options
        try:
            subprocess.call(args)
        except KeyboardInterrupt:
            pass

    def shuffle(self, cmd):
        cdir = self.get_cassandra_dir()
        shuffle = os.path.join(cdir, 'bin', 'cassandra-shuffle')
        host = self.address()
        args = [ shuffle, '-h', host, '-p', str(self.jmx_port) ] + [ cmd ]
        try:
            subprocess.call(args)
        except KeyboardInterrupt:
            pass

    def data_size(self, live_data=True):
        size = 0
        if live_data:
            for ks in self.list_keyspaces():
                size += sum((os.path.getsize(path) for path in self.get_sstables(ks, "")))
        else:
            for ks in self.list_keyspaces():
                for root, dirs, files in os.walk(os.path.join(self.get_path(), 'data', ks)):
                    size += sum((os.path.getsize(os.path.join(root, f)) for f in files if os.path.isfile(os.path.join(root, f))))
        return size

    def flush(self):
        self.nodetool("flush")

    def compact(self):
        self.nodetool("compact")

    def drain(self):
        self.nodetool("drain")

    def repair(self):
        self.nodetool("repair")

    def move(self, new_token):
        self.nodetool("move " + str(new_token))

    def cleanup(self):
        self.nodetool("cleanup")

    def version(self):
        self.nodetool("version");

    def decommission(self):
        self.nodetool("decommission")
        self.status = Status.DECOMMISIONNED
        self.__update_config()

    def removeToken(self, token):
        self.nodetool("removeToken " + str(token))

    def import_config_files(self):
        self.__update_config()

        conf_dir = os.path.join(self.get_cassandra_dir(), 'conf')
        for name in os.listdir(conf_dir):
            filename = os.path.join(conf_dir, name)
            if os.path.isfile(filename):
                shutil.copy(filename, self.get_conf_dir())

        self.__update_yaml()
        version = self.cluster.version()
        #loggers changed > 2.1
        if float(version[:version.index('.')+2]) < 2.1:
            self.__update_log4j()
        else:
            self.__update_logback()
        self.__update_envfile()

    def import_bin_files(self):
        bin_dir = os.path.join(self.get_cassandra_dir(), 'bin')
        for name in os.listdir(bin_dir):
            filename = os.path.join(bin_dir, name)
            if os.path.isfile(filename):
                shutil.copy(filename, self.get_bin_dir())

    def _save(self):
        self.__update_yaml()
        version = self.cluster.version()
        #loggers changed > 2.1
        if float(version[:version.index('.')+2]) < 2.1:
            self.__update_log4j()
        else:
            self.__update_logback()
        self.__update_envfile()

    def __update_config(self):
        dir_name = self.get_path()
        if not os.path.exists(dir_name):
            os.mkdir(dir_name)
            for dir in self.__get_diretories():
                os.mkdir(os.path.join(dir_name, dir))

        filename = os.path.join(dir_name, 'node.conf')
        values = {
            'name' : self.name,
            'status' : self.status,
            'auto_bootstrap' : self.auto_bootstrap,
            'interfaces' : self.network_interfaces,
            'jmx_port' : self.jmx_port,
            'config_options' : self.__config_options,
        }
        if self.pid:
            values['pid'] = self.pid
        if self.initial_token:
            values['initial_token'] = self.initial_token
        if self.__cassandra_dir is not None:
            values['cassandra_dir'] = self.__cassandra_dir
        if self.data_center:
            values['data_center'] = self.data_center
        if self.remote_debug_port:
            values['remote_debug_port'] = self.remote_debug_port
        with open(filename, 'w') as f:
            yaml.safe_dump(values, f)

    def __update_yaml(self):
        conf_file = os.path.join(self.get_conf_dir(), common.CASSANDRA_CONF)
        with open(conf_file, 'r') as f:
            data = yaml.load(f)

        data['cluster_name'] = self.cluster.name
        data['auto_bootstrap'] = self.auto_bootstrap
        data['initial_token'] = self.initial_token
        if 'seeds' in data:
            # cassandra 0.7
            data['seeds'] = self.cluster.get_seeds()
        else:
            # cassandra 0.8
            data['seed_provider'][0]['parameters'][0]['seeds'] = ','.join(self.cluster.get_seeds())
        data['listen_address'], data['storage_port'] = self.network_interfaces['storage']
        data['rpc_address'], data['rpc_port'] = self.network_interfaces['thrift']
        if self.network_interfaces['binary'] is not None and self.cluster.version() >= "1.2":
            _, data['native_transport_port'] = self.network_interfaces['binary']

        data['data_file_directories'] = [ os.path.join(self.get_path(), 'data') ]
        data['commitlog_directory'] = os.path.join(self.get_path(), 'commitlogs')
        data['saved_caches_directory'] = os.path.join(self.get_path(), 'saved_caches')

        if self.cluster.partitioner:
            data['partitioner'] = self.cluster.partitioner

        full_options = dict(self.cluster._config_options.items() + self.__config_options.items()) # last win and we want node options to win
        for name in full_options:
            value = full_options[name]
            if value is None:
                try:
                    del data[name]
                except KeyError:
                    # it is fine to remove a key not there:w
                    pass
            else:
                data[name] = full_options[name]

        with open(conf_file, 'w') as f:
            yaml.safe_dump(data, f, default_flow_style=False)

    def __update_log4j(self):
        append_pattern='log4j.appender.R.File='
        conf_file = os.path.join(self.get_conf_dir(), common.LOG4J_CONF)
        log_file = os.path.join(self.get_path(), 'logs', 'system.log')
        common.replace_in_file(conf_file, append_pattern, append_pattern + log_file)

        # Setting the right log level

        # Replace the global log level
        if self.__global_log_level is not None:
            append_pattern='log4j.rootLogger='
            common.replace_in_file(conf_file, append_pattern, append_pattern + self.__global_log_level + ',stdout,R')

        # Class specific log levels
        for class_name in self.__classes_log_level:
            logger_pattern='log4j.logger'
            full_logger_pattern = logger_pattern + '.' + class_name + '='
            common.replace_or_add_into_file_tail(conf_file, full_logger_pattern, full_logger_pattern + self.__classes_log_level[class_name])

    def __update_logback(self):
        append_pattern='<file>.*</file>'
        conf_file = os.path.join(self.get_conf_dir(), common.LOGBACK_CONF)
        log_file = os.path.join(self.get_path(), 'logs', 'system.log')
        common.replace_in_file(conf_file, append_pattern, '<file>' + log_file + '</file>')

        append_pattern='<fileNamePattern>.*</fileNamePattern>'
        common.replace_in_file(conf_file, append_pattern, '<fileNamePattern>' + log_file + '.%i.zip</fileNamePattern>')

        # Setting the right log level

        # Replace the global log level
        if self.__global_log_level is not None:
            append_pattern='<root level=".*">'
            common.replace_in_file(conf_file, append_pattern, '<root level="' + self.__global_log_level + '">')

        # Class specific log levels
        for class_name in self.__classes_log_level:
            logger_pattern='\t<logger name="'
            full_logger_pattern = logger_pattern + class_name + '" level=".*"/>'
            common.replace_or_add_into_file_tail(conf_file, full_logger_pattern, logger_pattern + class_name + '" level="' + self.__classes_log_level[class_name] + '"/>')

    def __update_envfile(self):
        jmx_port_pattern='JMX_PORT='
        remote_debug_port_pattern='address='
        conf_file = os.path.join(self.get_conf_dir(), common.CASSANDRA_ENV)
        common.replace_in_file(conf_file, jmx_port_pattern, jmx_port_pattern + self.jmx_port)
        if self.remote_debug_port != '0':
            common.replace_in_file(conf_file, remote_debug_port_pattern, 'JVM_OPTS="$JVM_OPTS -Xdebug -Xnoagent -Xrunjdwp:transport=dt_socket,server=y,suspend=n,address=' + str(self.remote_debug_port) + '"')

        if self.cluster.version() < '2.0.1':
            common.replace_in_file(conf_file, "-Xss", '    JVM_OPTS="$JVM_OPTS -Xss228k"')

    def __update_status(self):
        if self.pid is None:
            if self.status == Status.UP or self.status == Status.DECOMMISIONNED:
                self.status = Status.DOWN
            return

        old_status = self.status
        try:
            os.kill(self.pid, 0)
        except OSError, err:
            if err.errno == errno.ESRCH:
                # not running
                if self.status == Status.UP or self.status == Status.DECOMMISIONNED:
                    self.status = Status.DOWN
            elif err.errno == errno.EPERM:
                # no permission to signal this process
                if self.status == Status.UP or self.status == Status.DECOMMISIONNED:
                    self.status = Status.DOWN
            else:
                # some other error
                raise err
        else:
            if self.status == Status.DOWN or self.status == Status.UNINITIALIZED:
                self.status = Status.UP

        if not old_status == self.status:
            if old_status == Status.UP and self.status == Status.DOWN:
                self.pid = None
            self.__update_config()

    def __get_diretories(self):
        dirs = {}
        for i in ['data', 'commitlogs', 'saved_caches', 'logs', 'conf', 'bin']:
            dirs[i] = os.path.join(self.get_path(), i)
        return dirs

    def __get_status_string(self):
        if self.status == Status.UNINITIALIZED:
            return "%s (%s)" % (Status.DOWN, "Not initialized")
        else:
            return self.status

    def _update_pid(self, process):
        pidfile = os.path.join(self.get_path(), 'cassandra.pid')
        try:
            with open(pidfile, 'r') as f:
                self.pid = int(f.readline().strip())
        except IOError:
            raise NodeError('Problem starting node %s' % self.name, process)
        self.__update_status()

    def __gather_sstables(self, datafile=None, keyspace=None, columnfamilies=None):
        datafiles = []
        if keyspace is None:
            for k in self.list_keyspaces():
                datafiles = datafiles + self.get_sstables(k, "")
        elif datafile is None:
            if columnfamilies is None:
                datafiles = datafiles + self.get_sstables(keyspace, "")
            else:
                for cf in columnfamilies:
                    datafiles = datafiles + self.get_sstables(keyspace, cf)
        else:
            keyspace_dir = os.path.join(self.get_path(), 'data', keyspace)
            datafiles = [ os.path.join(keyspace_dir, datafile) ]

        return datafiles
