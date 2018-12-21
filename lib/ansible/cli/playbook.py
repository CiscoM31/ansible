# (c) 2012, Michael DeHaan <michael.dehaan@gmail.com>
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import json
import os
import stat

from ansible.cli import CLI
from ansible.errors import AnsibleError, AnsibleOptionsError
from ansible.executor.playbook_executor import PlaybookExecutor
from ansible.playbook.block import Block
from ansible.playbook.play_context import PlayContext
from ansible.utils.display import Display

display = Display()


class PlaybookCLI(CLI):
    ''' the tool to run *Ansible playbooks*, which are a configuration and multinode deployment system.
        See the project home page (https://docs.ansible.com) for more information. '''

    def parse(self):

        # create parser for CLI options
        parser = CLI.base_parser(
            usage="%prog [options] playbook.yml [playbook2 ...]",
            connect_opts=True,
            meta_opts=True,
            runas_opts=True,
            subset_opts=True,
            check_opts=True,
            inventory_opts=True,
            runtask_opts=True,
            vault_opts=True,
            fork_opts=True,
            module_opts=True,
            desc="Runs Ansible playbooks, executing the defined tasks on the targeted hosts.",
        )

        # ansible playbook specific opts
        parser.add_option('--list-tasks', dest='listtasks', action='store_true',
                          help="list all tasks that would be executed")
        parser.add_option('--list-tasks-with-path', dest='listtaskswithpath', action='store_true',
                          help="list all tasks that would be executed along with its absolute path and line number")
        parser.add_option('--list-tasks-json', dest='listtasksjson', action='store_true',
                          help="list all tasks that would be executed in JSON format")
        parser.add_option('--list-tags', dest='listtags', action='store_true',
                          help="list all available tags")
        parser.add_option('--step', dest='step', action='store_true',
                          help="one-step-at-a-time: confirm each task before running")
        parser.add_option('--start-at-task', dest='start_at_task',
                          help="start the playbook at the task matching this name")

        self.parser = parser
        super(PlaybookCLI, self).parse()

        if len(self.args) == 0:
            raise AnsibleOptionsError("You must specify a playbook file to run")

        display.verbosity = self.options.verbosity
        self.validate_conflicts(runas_opts=True, vault_opts=True, fork_opts=True)

    def run(self):

        super(PlaybookCLI, self).run()

        # Note: slightly wrong, this is written so that implicit localhost
        # Manage passwords
        sshpass = None
        becomepass = None
        passwords = {}

        # initial error check, to make sure all specified playbooks are accessible
        # before we start running anything through the playbook executor
        for playbook in self.args:
            if not os.path.exists(playbook):
                raise AnsibleError("the playbook: %s could not be found" % playbook)
            if not (os.path.isfile(playbook) or stat.S_ISFIFO(os.stat(playbook).st_mode)):
                raise AnsibleError("the playbook: %s does not appear to be a file" % playbook)

        # don't deal with privilege escalation or passwords when we don't need to
        if not self.options.listhosts and not self.options.listtasks and not self.options.listtaskswithpath and not self.options.listtasksjson and not self.options.listtags and not self.options.syntax:
            self.normalize_become_options()
            (sshpass, becomepass) = self.ask_passwords()
            passwords = {'conn_pass': sshpass, 'become_pass': becomepass}

        loader, inventory, variable_manager = self._play_prereqs(self.options)

        # (which is not returned in list_hosts()) is taken into account for
        # warning if inventory is empty.  But it can't be taken into account for
        # checking if limit doesn't match any hosts.  Instead we don't worry about
        # limit if only implicit localhost was in inventory to start with.
        #
        # Fix this when we rewrite inventory by making localhost a real host (and thus show up in list_hosts())
        hosts = CLI.get_host_list(inventory, self.options.subset)

        # flush fact cache if requested
        if self.options.flush_cache:
            self._flush_cache(inventory, variable_manager)

        # create the playbook executor, which manages running the plays via a task queue manager
        pbex = PlaybookExecutor(playbooks=self.args, inventory=inventory, variable_manager=variable_manager, loader=loader, options=self.options,
                                passwords=passwords)

        results = pbex.run()

        if isinstance(results, list):
            for p in results:
                playbook_name = p['playbook']
                dir = os.path.realpath(os.path.dirname(p['playbook']))
                playbook_json = PlaybookCliJSON(playbook_name, dir)
                play_list = []

                if not self.options.listtasksjson:
                    display.display('\nplaybook: %s' % playbook_name)

                for idx, play in enumerate(p['plays']):
                    if play._included_path is not None:
                        loader.set_basedir(play._included_path)
                    else:
                        pb_dir = os.path.realpath(os.path.dirname(p['playbook']))
                        loader.set_basedir(pb_dir)

                    curr_play = PlayCliJSON(play=play)

                    msg = "\n  play #%d (%s): %s" % (idx + 1, ','.join(play.hosts), play.name)
                    mytags = set(play.tags)
                    msg += '\tTAGS: [%s]' % (','.join(mytags))

                    if self.options.listtaskswithpath:
                        msg += '\tPATH: [%s]' % play.get_path()

                    if self.options.listhosts:
                        playhosts = set(inventory.get_hosts(play.hosts))
                        msg += "\n    pattern: %s\n    hosts (%d):" % (play.hosts, len(playhosts))
                        for host in playhosts:
                            msg += "\n      %s" % host

                    if not self.options.listtasksjson:
                        display.display(msg)

                    all_tags = set()
                    if self.options.listtags or self.options.listtasks or self.options.listtaskswithpath or self.options.listtasksjson:
                        taskmsg = ''
                        task_list = []
                        block_task_list = []
                        if self.options.listtasks or self.options.listtaskswithpath or self.options.listtasksjson:
                            taskmsg = '    tasks:\n'

                        def _process_block(b):
                            taskmsg = ''
                            task_list = []
                            for task in b.block:
                                if isinstance(task, Block):
                                    block_taskmsg, block_task_list = _process_block(task)
                                    task_list += block_task_list
                                    taskmsg += block_taskmsg
                                else:
                                    if task.action == 'meta':
                                        continue

                                    all_tags.update(task.tags)
                                    if self.options.listtasks or self.options.listtaskswithpath or self.options.listtasksjson:
                                        cur_tags = list(mytags.union(set(task.tags)))
                                        cur_tags.sort()

                                        if task.name:
                                            taskmsg += "      %s" % task.get_name()
                                        else:
                                            taskmsg += "      %s" % task.action
                                        taskmsg += "\tTAGS: [%s]" % ', '.join(cur_tags)

                                        if self.options.listtaskswithpath:
                                            taskmsg += "\tPATH: [%s]" % task.get_path()
                                        taskmsg += "\n"

                                        if self.options.listtasksjson:
                                            task_list.append(TaskCliJSON(task))

                            return taskmsg, task_list

                        all_vars = variable_manager.get_vars(play=play)
                        play_context = PlayContext(play=play, options=self.options)
                        for block in play.compile():
                            block = block.filter_tagged_tasks(play_context, all_vars)
                            if not block.has_tasks():
                                continue
                            block_taskmsg, block_task_list = _process_block(block)
                            taskmsg += block_taskmsg
                            task_list += block_task_list

                        if self.options.listtags:
                            cur_tags = list(mytags.union(all_tags))
                            cur_tags.sort()
                            taskmsg += "      TASK TAGS: [%s]\n" % ', '.join(cur_tags)

                        if self.options.listtasksjson:
                            curr_play.set_tasks(task_list)
                        else:
                            display.display(taskmsg)

                    if self.options.listtasksjson:
                        play_list.append(curr_play)
                if self.options.listtasksjson:
                    playbook_json.set_plays(play_list)
                    display.display(json.dumps(playbook_json.json_repr(), indent=2, cls=CLIEncoder))
            return 0
        else:
            return results

    def _flush_cache(self, inventory, variable_manager):
        for host in inventory.list_hosts():
            hostname = host.get_name()
            variable_manager.clear_facts(hostname)


############################################################################
# Classes below are used for CLI option --list-tasks-json
# For a given playbook, a small subset of desired
# information is extracted and output in JSON
############################################################################

class CLIEncoder(json.JSONEncoder):
    """
    This ComplexEncoder is used to help encode python classes to JSON format
    """
    def default(self, obj):
        if hasattr(obj, 'json_cli_repr'):
            return remove_nulls(obj.json_cli_repr())
        else:
            try:
                return json.JSONEncoder.default(self, obj)
            except TypeError:
                pass

def remove_nulls(dict):
    if dict is None:
        return dict
    return {k: v for k, v in dict.items() if v is not None}

class PlaybookCliJSON:
    """
    A JSON Class representation for Ansible playbook
    """

    def __init__(self, playbook, playbook_dir):

        self.playbook = playbook
        self.playbook_dir = playbook_dir

    def __init__(self, playbook, playbook_dir, plays=None):
        self.playbook = playbook
        self.plays = plays
        self.playbook_dir = playbook_dir

    def add_play(self, play):
        """
        Add play of class PlayCliJSON
        """
        if self.plays is None:
            self.plays = [play]
        else:
            self.plays.append(play)

    def set_plays(self, play_list):
        """
        Set plays with list containing class PlayCliJSON
        """
        self.plays = play_list

    def json_repr(self):
        return self.__dict__
