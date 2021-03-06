# Copyright 2013: Mirantis Inc.
# All Rights Reserved.
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

"""Rally command: task"""

from __future__ import print_function
import collections
import json
import os
import sys
import webbrowser

import jsonschema
from oslo_utils import uuidutils
import six
from six.moves.urllib import parse as urlparse

from rally.cli import cliutils
from rally.cli import envutils
from rally.common import fileutils
from rally.common.i18n import _
from rally.common.io import junit
from rally.common import logging
from rally.common import utils as rutils
from rally.common import version
from rally.common import yamlutils as yaml
from rally import consts
from rally import exceptions
from rally import plugins
from rally.task import exporter
from rally.task.processing import plot
from rally.task.processing import utils as putils
from rally.task import utils as tutils


LOG = logging.getLogger(__name__)


class FailedToLoadTask(exceptions.RallyException):
    error_code = 472
    msg_fmt = _("Invalid %(source)s passed:\n\n\t %(msg)s")


class FailedToLoadResults(exceptions.RallyException):
    error_code = 529
    msg_fmt = _("ERROR: Invalid task result format in %(source)s\n\n\t%(msg)s")


class TaskCommands(object):
    """Set of commands that allow you to manage benchmarking tasks and results.

    """

    def _load_and_validate_task(self, api, task_file, args_file=None,
                                raw_args=None):
        """Load, render and validate tasks template from file with passed args.

        :param task_file: Path to file with input task
        :param raw_args: JSON or YAML representation of dict with args that
            will be used to render input task with jinja2
        :param args_file: Path to file with JSON or YAML representation
            of dict, that will be used to render input with jinja2. If both
            specified task_args and task_args_file they will be merged.
            raw_args has bigger priority so it will update values
            from args_file.
        :returns: Str with loaded and rendered task
        """

        print(cliutils.make_header("Preparing input task"))

        if not os.path.isfile(task_file):
            raise FailedToLoadTask(source="--task",
                                   msg="File '%s' doesn't exist." % task_file)
        with open(task_file) as f:
            input_task = f.read()
            task_dir = os.path.expanduser(os.path.dirname(task_file)) or "./"

        task_args = {}
        if args_file:
            if not os.path.isfile(args_file):
                raise FailedToLoadTask(
                    source="--task-args-file",
                    msg="File '%s' doesn't exist." % args_file)
            with open(args_file) as f:
                try:
                    task_args.update(yaml.safe_load(f.read()))
                except yaml.ParserError as e:
                    raise FailedToLoadTask(
                        source="--task-args-file",
                        msg="File '%s' has to be YAML or JSON. Details:\n\n%s"
                            % (args_file, e))
        if raw_args:
            try:
                data = yaml.safe_load(raw_args)
                if isinstance(data, (six.text_type, six.string_types)):
                    raise yaml.ParserError("String '%s' doesn't look like a "
                                           "dictionary." % raw_args)
                task_args.update(data)
            except yaml.ParserError as e:
                args = [keypair.split("=", 1)
                        for keypair in raw_args.split(",")]
                if len([a for a in args if len(a) != 1]) != len(args):
                    raise FailedToLoadTask(
                        source="--task-args",
                        msg="Value has to be YAML or JSON. Details:\n\n%s" % e)
                else:
                    task_args.update(dict(args))

        try:
            rendered_task = api.task.render_template(task_template=input_task,
                                                     template_dir=task_dir,
                                                     **task_args)
        except Exception as e:
            raise FailedToLoadTask(
                source="--task",
                msg="Failed to render task template.\n\n%s" % e)

        print(_("Task is:\n%s\n") % rendered_task.strip())
        try:
            parsed_task = yaml.safe_load(rendered_task)
        except Exception as e:
            raise FailedToLoadTask(
                source="--task",
                msg="Wrong format of rendered input task. It should be YAML or"
                    " JSON. Details:\n\n%s" % e)

        print(_("Task syntax is correct :)"))
        return parsed_task

    @cliutils.args("--deployment", dest="deployment", type=str,
                   metavar="<uuid>", required=False,
                   help="UUID or name of a deployment.")
    @cliutils.args("--task", "--filename", metavar="<path>",
                   dest="task_file",
                   help="Path to the input task file.")
    @cliutils.args("--task-args", metavar="<json>", dest="task_args",
                   help="Input task args (JSON dict). These args are used "
                        "to render the Jinja2 template in the input task.")
    @cliutils.args("--task-args-file", metavar="<path>", dest="task_args_file",
                   help="Path to the file with input task args (dict in "
                        "JSON/YAML). These args are used "
                        "to render the Jinja2 template in the input task.")
    @envutils.with_default_deployment(cli_arg_name="deployment")
    @plugins.ensure_plugins_are_loaded
    def validate(self, api, task_file, deployment=None, task_args=None,
                 task_args_file=None):
        """Validate a task configuration file.

        This will check that task configuration file has valid syntax and
        all required options of scenarios, contexts, SLA and runners are set.

        If both task_args and task_args_file are specified, they will
        be merged. task_args has a higher priority so it will override
        values from task_args_file.

        :param task_file: Path to the input task file.
        :param task_args: Input task args (JSON dict). These args are
                          used to render the Jinja2 template in the
                          input task.
        :param task_args_file: Path to the file with input task args
                               (dict in JSON/YAML). These args are
                               used to render the Jinja2 template in
                               the input task.
        :param deployment: UUID or name of the deployment
        """

        task = self._load_and_validate_task(api, task_file, raw_args=task_args,
                                            args_file=task_args_file)

        api.task.validate(deployment=deployment, config=task)

        print(_("Task config is valid :)"))

    @cliutils.args("--deployment", dest="deployment", type=str,
                   metavar="<uuid>", required=False,
                   help="UUID or name of a deployment.")
    @cliutils.args("--task", "--filename", metavar="<path>",
                   dest="task_file",
                   help="Path to the input task file.")
    @cliutils.args("--task-args", dest="task_args", metavar="<json>",
                   help="Input task args (JSON dict). These args are used "
                        "to render the Jinja2 template in the input task.")
    @cliutils.args("--task-args-file", dest="task_args_file", metavar="<path>",
                   help="Path to the file with input task args (dict in "
                        "JSON/YAML). These args are used "
                        "to render the Jinja2 template in the input task.")
    @cliutils.args("--tag", help="Tag for this task")
    @cliutils.args("--no-use", action="store_false", dest="do_use",
                   help="Don't set new task as default for future operations.")
    @cliutils.args("--abort-on-sla-failure", action="store_true",
                   dest="abort_on_sla_failure",
                   help="Abort the execution of a benchmark scenario when"
                        "any SLA check for it fails.")
    @envutils.with_default_deployment(cli_arg_name="deployment")
    @plugins.ensure_plugins_are_loaded
    def start(self, api, task_file, deployment=None, task_args=None,
              task_args_file=None, tag=None, do_use=False,
              abort_on_sla_failure=False):
        """Start benchmark task.

        If both task_args and task_args_file are specified, they will
        be merged. task_args has a higher priority so it will override
        values from task_args_file.

        :param task_file: Path to the input task file.
        :param task_args: Input task args (JSON dict). These args are
                          used to render the Jinja2 template in the
                          input task.
        :param task_args_file: Path to the file with input task args
                               (dict in JSON/YAML). These args are
                               used to render the Jinja2 template in
                               the input task.
        :param deployment: UUID or name of the deployment
        :param tag: optional tag for this task
        :param do_use: if True, the new task will be stored as the default one
                       for future operations
        :param abort_on_sla_failure: if True, the execution of a benchmark
                                     scenario will stop when any SLA check
                                     for it fails
        """

        input_task = self._load_and_validate_task(api, task_file,
                                                  raw_args=task_args,
                                                  args_file=task_args_file)
        print("Running Rally version", version.version_string())

        try:
            task_instance = api.task.create(deployment=deployment, tag=tag)

            print(cliutils.make_header(
                _("Task %(tag)s %(uuid)s: started")
                % {"uuid": task_instance["uuid"],
                   "tag": task_instance["tag"]}))
            print("Benchmarking... This can take a while...\n")
            print("To track task status use:\n")
            print("\trally task status\n\tor\n\trally task detailed\n")

            if do_use:
                self.use(api, task_instance["uuid"])

            api.task.start(deployment=deployment, config=input_task,
                           task=task_instance["uuid"],
                           abort_on_sla_failure=abort_on_sla_failure)

        except exceptions.DeploymentNotFinishedStatus as e:
            print(_("Cannot start a task on unfinished deployment: %s") % e)
            return 1

        self.detailed(api, task_id=task_instance["uuid"])

    @cliutils.args("--uuid", type=str, dest="task_id", help="UUID of task.")
    @envutils.with_default_task_id
    @cliutils.args(
        "--soft", action="store_true",
        help="Abort task after current scenario finishes execution.")
    def abort(self, api, task_id=None, soft=False):
        """Abort a running benchmarking task.

        :param task_id: Task uuid
        :param soft: if set to True, task should be aborted after execution of
                     current scenario
        """
        if soft:
            print("INFO: please be informed that soft abort won't stop "
                  "a running scenario, but will prevent new ones from "
                  "starting. If you are running task with only one "
                  "scenario, soft abort will not help at all.")

        api.task.abort(task_uuid=task_id, soft=soft, async=False)

        print("Task %s successfully stopped." % task_id)

    @cliutils.args("--uuid", type=str, dest="task_id", help="UUID of task")
    @envutils.with_default_task_id
    def status(self, api, task_id=None):
        """Display the current status of a task.

        :param task_id: Task uuid
        Returns current status of task
        """

        task = api.task.get(task_id=task_id)
        print(_("Task %(task_id)s: %(status)s")
              % {"task_id": task_id, "status": task["status"]})

    @cliutils.args("--uuid", type=str, dest="task_id",
                   help=("UUID of task. If --uuid is \"last\" the results of "
                         " the most recently created task will be displayed."))
    @cliutils.args("--iterations-data", dest="iterations_data",
                   action="store_true",
                   help="Print detailed results for each iteration.")
    @envutils.with_default_task_id
    def detailed(self, api, task_id=None, iterations_data=False):
        """Print detailed information about given task.

        :param task_id: str, task uuid
        :param iterations_data: bool, include results for each iteration
        """
        task = api.task.get_detailed(task_id=task_id, extended_results=True)

        if not task:
            print("The task %s can not be found" % task_id)
            return 1

        print()
        print("-" * 80)
        print(_("Task %(task_id)s: %(status)s")
              % {"task_id": task_id, "status": task["status"]})

        if task["status"] == consts.TaskStatus.CRASHED or task["status"] == (
                consts.TaskStatus.VALIDATION_FAILED):
            print("-" * 80)
            verification = yaml.safe_load(task["verification_log"])
            if logging.is_debug():
                print(yaml.safe_load(verification["trace"]))
            else:
                print(verification["etype"])
                print(verification["msg"])
                print(_("\nFor more details run:\nrally -d task detailed %s")
                      % task["uuid"])
            return 0
        elif task["status"] not in [consts.TaskStatus.FINISHED,
                                    consts.TaskStatus.ABORTED]:
            print("-" * 80)
            print(_("\nThe task %s marked as '%s'. Results "
                    "available when it is '%s'.") % (
                task_id, task["status"], consts.TaskStatus.FINISHED))
            return 0

        for result in task["results"]:
            key = result["key"]
            print("-" * 80)
            print()
            print("test scenario %s" % key["name"])
            print("args position %s" % key["pos"])
            print("args values:")
            print(json.dumps(key["kw"], indent=2))
            print()

            iterations = []
            iterations_headers = ["iteration", "duration"]
            iterations_actions = []
            output = []
            task_errors = []
            if iterations_data:
                atomic_merger = putils.AtomicMerger(result["info"]["atomic"])
                atomic_names = atomic_merger.get_merged_names()
                for i, atomic_name in enumerate(atomic_names, 1):
                    action = "%i. %s" % (i, atomic_name)
                    iterations_headers.append(action)
                    iterations_actions.append((atomic_name, action))

            for idx, itr in enumerate(result["iterations"], 1):

                if iterations_data:
                    row = {"iteration": idx, "duration": itr["duration"]}
                    for name, action in iterations_actions:
                        atomic_actions = (
                            atomic_merger.merge_atomic_actions(
                                itr["atomic_actions"]))
                        row[action] = atomic_actions.get(name, 0)
                    iterations.append(row)

                if "output" in itr:
                    iteration_output = itr["output"]
                else:
                    iteration_output = {"additive": [], "complete": []}

                    # NOTE(amaretskiy): "scenario_output" is supported
                    #   for backward compatibility
                    if ("scenario_output" in itr
                            and itr["scenario_output"]["data"]):
                        iteration_output["additive"].append(
                            {"data": itr["scenario_output"]["data"].items(),
                             "title": "Scenario output",
                             "description": "",
                             "chart_plugin": "StackedArea"})

                for idx, additive in enumerate(iteration_output["additive"]):
                    if len(output) <= idx + 1:
                        output_table = plot.charts.OutputStatsTable(
                            result["info"], title=additive["title"])
                        output.append(output_table)
                    output[idx].add_iteration(additive["data"])

                if itr.get("error"):
                    task_errors.append(TaskCommands._format_task_error(itr))

            self._print_task_errors(task_id, task_errors)

            cols = plot.charts.MainStatsTable.columns
            float_cols = result["info"]["stat"]["cols"][1:7]
            formatters = dict(zip(float_cols,
                                  [cliutils.pretty_float_formatter(col, 3)
                                   for col in float_cols]))
            rows = [dict(zip(cols, r)) for r in result["info"]["stat"]["rows"]]
            cliutils.print_list(rows,
                                fields=cols,
                                formatters=formatters,
                                table_label="Response Times (sec)",
                                sortby_index=None)
            print()

            if iterations_data:
                formatters = dict(zip(iterations_headers[1:],
                                      [cliutils.pretty_float_formatter(col, 3)
                                       for col in iterations_headers[1:]]))
                cliutils.print_list(iterations,
                                    fields=iterations_headers,
                                    table_label="Atomics per iteration",
                                    formatters=formatters)
                print()

            if output:
                cols = plot.charts.OutputStatsTable.columns
                float_cols = cols[1:7]
                formatters = dict(zip(float_cols,
                                  [cliutils.pretty_float_formatter(col, 3)
                                   for col in float_cols]))

                for out in output:
                    data = out.render()
                    rows = [dict(zip(cols, r)) for r in data["data"]["rows"]]
                    if rows:
                        # NOTE(amaretskiy): print title explicitly because
                        #     prettytable fails if title length is too long
                        print(data["title"])
                        cliutils.print_list(rows, fields=cols,
                                            formatters=formatters)
                        print()

            print(_("Load duration: %s") %
                  rutils.format_float_to_str(result["info"]["load_duration"]))
            print(_("Full duration: %s") %
                  rutils.format_float_to_str(result["info"]["full_duration"]))

            print("\nHINTS:")
            print(_("* To plot HTML graphics with this data, run:"))
            print("\trally task report %s --out output.html\n" % task["uuid"])
            print(_("* To generate a JUnit report, run:"))
            print("\trally task report %s --junit --out output.xml\n" %
                  task["uuid"])
            print(_("* To get raw JSON output of task results, run:"))
            print("\trally task results %s\n" % task["uuid"])

    @cliutils.args("--uuid", type=str, dest="task_id", help="UUID of task.")
    @envutils.with_default_task_id
    @cliutils.suppress_warnings
    def results(self, api, task_id=None):
        """Display raw task results.

        This will produce a lot of output data about every iteration.

        :param task_id: Task uuid
        """
        task = api.task.get(task_id=task_id)
        finished_statuses = (consts.TaskStatus.FINISHED,
                             consts.TaskStatus.ABORTED)
        if task["status"] not in finished_statuses:
            print(_("Task status is %s. Results available when it is one "
                    "of %s.") % (task["status"], ", ".join(finished_statuses)))
            return 1

        # TODO(chenhb): Ensure `rally task results` puts out old format.
        for result in task["results"]:
            for itr in result["data"]["raw"]:
                itr["atomic_actions"] = collections.OrderedDict(
                    tutils.WrapperForAtomicActions(
                        itr["atomic_actions"]).items()
                )

        results = [{"key": x["key"], "result": x["data"]["raw"],
                    "sla": x["data"]["sla"],
                    "hooks": x["data"].get("hooks", []),
                    "load_duration": x["data"]["load_duration"],
                    "full_duration": x["data"]["full_duration"],
                    "created_at": x["created_at"]}
                   for x in task["results"]]

        print(json.dumps(results, sort_keys=False, indent=4))

    @cliutils.args("--deployment", dest="deployment", type=str,
                   metavar="<uuid>", required=False,
                   help="UUID or name of a deployment.")
    @cliutils.args("--all-deployments", action="store_true",
                   dest="all_deployments",
                   help="List tasks from all deployments.")
    @cliutils.args("--status", type=str, dest="status",
                   help="List tasks with specified status."
                   " Available statuses: %s" % ", ".join(consts.TaskStatus))
    @cliutils.args("--uuids-only", action="store_true",
                   dest="uuids_only", help="List task UUIDs only.")
    @envutils.with_default_deployment(cli_arg_name="deployment")
    def list(self, api, deployment=None, all_deployments=False, status=None,
             uuids_only=False):
        """List tasks, started and finished.

        Displayed tasks can be filtered by status or deployment.  By
        default 'rally task list' will display tasks from the active
        deployment without filtering by status.

        :param deployment: UUID or name of deployment
        :param status: task status to filter by.
            Available task statuses are in rally.consts.TaskStatus
        :param all_deployments: display tasks from all deployments
        :param uuids_only: list task UUIDs only
        """

        filters = {}
        headers = ["uuid", "deployment_name", "created_at", "duration",
                   "status", "tag"]

        if status in consts.TaskStatus:
            filters.setdefault("status", status)
        elif status:
            print(_("Error: Invalid task status '%s'.\n"
                    "Available statuses: %s") % (
                  status, ", ".join(consts.TaskStatus)),
                  file=sys.stderr)
            return(1)

        if not all_deployments:
            filters.setdefault("deployment", deployment)

        task_list = api.task.list(**filters)

        if uuids_only:
            if task_list:
                cliutils.print_list(task_list, ["uuid"],
                                    print_header=False,
                                    print_border=False)
        elif task_list:
            cliutils.print_list(
                task_list,
                headers, sortby_index=headers.index("created_at"))
        else:
            if status:
                print(_("There are no tasks in '%s' status. "
                        "To run a new task, use:\n"
                        "\trally task start") % status)
            else:
                print(_("There are no tasks. To run a new task, use:\n"
                        "\trally task start"))

    def _load_task_results_file(self, api, task_id):
        """Load the json file which is created by `rally task results` """
        with open(os.path.expanduser(task_id), "r") as inp_js:
            tasks_results = json.load(inp_js)
            for result in tasks_results:
                try:
                    jsonschema.validate(
                        result,
                        api.task.TASK_RESULT_SCHEMA)
                    # TODO(chenhb): back compatible for atomic_actions
                    for r in result["result"]:
                        r["atomic_actions"] = list(
                            tutils.WrapperForAtomicActions(
                                r["atomic_actions"], r["timestamp"]))
                except jsonschema.ValidationError as e:
                    raise FailedToLoadResults(source=task_id,
                                              msg=six.text_type(e))

        return tasks_results

    @cliutils.args("--out", metavar="<path>",
                   type=str, dest="out", required=False,
                   help="Path to output file.")
    @cliutils.args("--open", dest="open_it", action="store_true",
                   help="Open the output in a browser.")
    @cliutils.args("--tasks", dest="tasks", nargs="+",
                   help="UUIDs of tasks, or JSON files with task results")
    @cliutils.suppress_warnings
    def trends(self, api, *args, **kwargs):
        """Generate workloads trends HTML report."""
        tasks = kwargs.get("tasks", []) or list(args)

        if not tasks:
            print(_("ERROR: At least one task must be specified"),
                  file=sys.stderr)
            return 1

        results = []
        for task_id in tasks:
            if os.path.exists(os.path.expanduser(task_id)):
                task_results = self._load_task_results_file(api, task_id)
            elif uuidutils.is_uuid_like(task_id):
                task_results = map(
                    lambda x: {"key": x["key"],
                               "sla": x["data"]["sla"],
                               "hooks": x["data"].get("hooks", []),
                               "result": x["data"]["raw"],
                               "load_duration": x["data"]["load_duration"],
                               "full_duration": x["data"]["full_duration"]},
                    api.task.get_detailed(task_id=task_id)["results"])
            else:
                print(_("ERROR: Invalid UUID or file name passed: %s")
                      % task_id, file=sys.stderr)
                return 1

            results.extend(task_results)

        result = plot.trends(results)

        out = kwargs.get("out")
        if out:
            output_file = os.path.expanduser(out)

            with open(output_file, "w+") as f:
                f.write(result)
            if kwargs.get("open_it"):
                webbrowser.open_new_tab("file://" + os.path.realpath(out))
        else:
            print(result)

    @cliutils.args("--tasks", dest="tasks", nargs="+",
                   help="UUIDs of tasks, or JSON files with task results")
    @cliutils.args("--out", metavar="<path>",
                   type=str, dest="out", required=False,
                   help="Path to output file.")
    @cliutils.args("--open", dest="open_it", action="store_true",
                   help="Open the output in a browser.")
    @cliutils.args("--html", dest="out_format",
                   action="store_const", const="html",
                   help="Generate the report in HTML.")
    @cliutils.args("--html-static", dest="out_format",
                   action="store_const", const="html_static",
                   help=("Generate the report in HTML with embedded "
                         "JS and CSS, so it will not depend on "
                         "Internet availability."))
    @cliutils.args("--junit", dest="out_format",
                   action="store_const", const="junit",
                   help="Generate the report in the JUnit format.")
    @envutils.default_from_global("tasks", envutils.ENV_TASK, "tasks")
    @cliutils.suppress_warnings
    def report(self, api, tasks=None, out=None, open_it=False,
               out_format="html"):
        """Generate report file for specified task.

        :param task_id: UUID, task identifier
        :param tasks: list, UUIDs od tasks or pathes files with tasks results
        :param out: str, output file name
        :param open_it: bool, whether to open output file in web browser
        :param out_format: output format (junit, html or html_static)
        """

        tasks = isinstance(tasks, list) and tasks or [tasks]

        results = []
        message = []
        processed_names = {}
        for task_file_or_uuid in tasks:
            if os.path.exists(os.path.expanduser(task_file_or_uuid)):
                tasks_results = self._load_task_results_file(
                    api, task_file_or_uuid)
            elif uuidutils.is_uuid_like(task_file_or_uuid):
                tasks_results = map(
                    lambda x: {"key": x["key"],
                               "sla": x["data"]["sla"],
                               "hooks": x["data"].get("hooks", []),
                               "result": x["data"]["raw"],
                               "load_duration": x["data"]["load_duration"],
                               "full_duration": x["data"]["full_duration"],
                               "created_at": x["created_at"]},
                    api.task.get_detailed(
                        task_id=task_file_or_uuid)["results"])
            else:
                print(_("ERROR: Invalid UUID or file name passed: %s"
                        ) % task_file_or_uuid,
                      file=sys.stderr)
                return 1

            for task_result in tasks_results:
                if task_result["key"]["name"] in processed_names:
                    processed_names[task_result["key"]["name"]] += 1
                    task_result["key"]["pos"] = processed_names[
                        task_result["key"]["name"]]
                else:
                    processed_names[task_result["key"]["name"]] = 0
                results.append(task_result)

        if out_format.startswith("html"):
            result = plot.plot(results,
                               include_libs=(out_format == "html_static"))
        elif out_format == "junit":
            test_suite = junit.JUnit("Rally test suite")
            for result in results:
                if isinstance(result["sla"], list):
                    message = ",".join([sla["detail"] for sla in
                                        result["sla"] if not sla["success"]])
                if message:
                    outcome = junit.JUnit.FAILURE
                else:
                    outcome = junit.JUnit.SUCCESS
                test_suite.add_test(result["key"]["name"],
                                    result["full_duration"], outcome, message)
            result = test_suite.to_xml()
        else:
            print(_("Invalid output format: %s") % out_format, file=sys.stderr)
            return 1

        if out:
            output_file = os.path.expanduser(out)

            with open(output_file, "w+") as f:
                f.write(result)
            if open_it:
                webbrowser.open_new_tab("file://" + os.path.realpath(out))
        else:
            print(result)

    @cliutils.args("--force", action="store_true", help="force delete")
    @cliutils.args("--uuid", type=str, dest="task_id", nargs="*",
                   metavar="<task-id>",
                   help="UUID of task or a list of task UUIDs.")
    @envutils.with_default_task_id
    def delete(self, api, task_id=None, force=False):
        """Delete task and its results.

        :param task_id: Task uuid or a list of task uuids
        :param force: Force delete or not
        """
        def _delete_single_task(tid, force):
            try:
                api.task.delete(task_uuid=tid, force=force)
                print("Successfully deleted task `%s`" % tid)
            except exceptions.TaskInvalidStatus as e:
                print(e)
                print("Use '--force' option to delete the task with vague "
                      "state.")

        if isinstance(task_id, list):
            for tid in task_id:
                _delete_single_task(tid, force)
        else:
            _delete_single_task(task_id, force)

    @cliutils.args("--uuid", type=str, dest="task_id", help="UUID of task.")
    @cliutils.args("--json", dest="tojson",
                   action="store_true",
                   help="Output in JSON format.")
    @envutils.with_default_task_id
    @cliutils.alias("sla_check")
    def sla_check_deprecated(self, api, task_id=None, tojson=False):
        """DEPRECATED since Rally 0.8.0, use `rally task sla-check` instead."""
        return self.sla_check(api, task_id=task_id, tojson=tojson)

    @cliutils.args("--uuid", type=str, dest="task_id", help="UUID of task.")
    @cliutils.args("--json", dest="tojson",
                   action="store_true",
                   help="Output in JSON format.")
    @envutils.with_default_task_id
    def sla_check(self, api, task_id=None, tojson=False):
        """Display SLA check results table.

        :param task_id: Task uuid.
        :returns: Number of failed criteria.
        """
        results = api.task.get_detailed(task_id=task_id)["results"]
        failed_criteria = 0
        data = []
        STATUS_PASS = "PASS"
        STATUS_FAIL = "FAIL"
        for result in results:
            key = result["key"]
            for sla in sorted(result["data"]["sla"],
                              key=lambda x: x["criterion"]):
                success = sla.pop("success")
                sla["status"] = success and STATUS_PASS or STATUS_FAIL
                sla["benchmark"] = key["name"]
                sla["pos"] = key["pos"]
                failed_criteria += int(not success)
                data.append(sla if tojson else rutils.Struct(**sla))
        if tojson:
            print(json.dumps(data, sort_keys=False))
        else:
            cliutils.print_list(data, ("benchmark", "pos", "criterion",
                                       "status", "detail"))
        return failed_criteria

    @cliutils.args("--uuid", type=str, dest="task_id",
                   help="UUID of the task")
    @cliutils.deprecated_args("--task", dest="task_id", type=str,
                              release="0.2.0", alternative="--uuid")
    def use(self, api, task_id):
        """Set active task.

        :param task_id: Task uuid.
        """
        print("Using task: %s" % task_id)
        api.task.get(task_id=task_id)
        fileutils.update_globals_file("RALLY_TASK", task_id)

    @cliutils.args("--uuid", dest="uuid", type=str,
                   required=True,
                   help="UUID of a the task.")
    @cliutils.args("--connection", dest="connection_string", type=str,
                   required=True,
                   help="Connection url to the task export system.")
    @plugins.ensure_plugins_are_loaded
    def export(self, api, uuid, connection_string):
        """Export task results to the custom task's exporting system.

        :param uuid: UUID of the task
        :param connection_string: string used to connect to the system
        """

        parsed_obj = urlparse.urlparse(connection_string)
        try:
            client = exporter.Exporter.get(parsed_obj.scheme)(
                connection_string)
        except exceptions.InvalidConnectionString as e:
            if logging.is_debug():
                LOG.exception(e)
            print(e)
            return 1
        except exceptions.PluginNotFound as e:
            if logging.is_debug():
                LOG.exception(e)
            msg = ("\nPlease check your connection string. The format of "
                   "`connection` should be plugin-name://"
                   "<user>:<pwd>@<full_address>:<port>/<path>.<type>")
            print(str(e) + msg)
            return 1

        try:
            client.export(uuid)
        except (IOError, exceptions.RallyException) as e:
            if logging.is_debug():
                LOG.exception(e)
            print(e)
            return 1
        print(_("Task %(uuid)s results was successfully exported to %("
                "connection)s using %(name)s plugin.") % {
                    "uuid": uuid,
                    "connection": connection_string,
                    "name": parsed_obj.scheme
        })

    @staticmethod
    def _print_task_errors(task_id, task_errors):
        print(cliutils.make_header("Task %s has %d error(s)" %
                                   (task_id, len(task_errors))))
        for err_data in task_errors:
            print(*err_data, sep="\n")
            print("-" * 80)

    @staticmethod
    def _format_task_error(data):
        error_type = _("Unknown type")
        error_message = _("Rally hasn't caught anything yet")
        error_traceback = _("No traceback available.")
        try:
            error_type = data["error"][0]
            error_message = data["error"][1]
            error_traceback = data["error"][2]
        except IndexError:
            pass
        return ("%(error_type)s: %(error_message)s\n" %
                {"error_type": error_type, "error_message": error_message},
                error_traceback)

    @cliutils.args("--file", dest="task_file", type=str, metavar="<path>",
                   required=True, help="JSON file with task results")
    @cliutils.args("--deployment", dest="deployment", type=str,
                   metavar="<uuid>", required=False,
                   help="UUID or name of a deployment.")
    @cliutils.args("--tag", help="Tag for this task")
    @envutils.with_default_deployment(cli_arg_name="deployment")
    @cliutils.alias("import")
    @cliutils.suppress_warnings
    def import_results(self, api, deployment=None, task_file=None, tag=None):
        """Import json results of a test into rally database

        :param task_file: list, pathes files with tasks results
        :param deployment: UUID or name of the deployment
        :param tag: optional tag for this task
        """

        if os.path.exists(os.path.expanduser(task_file)):
            tasks_results = self._load_task_results_file(api, task_file)
            task = api.task.import_results(deployment=deployment,
                                           task_results=tasks_results,
                                           tag=tag)
            print(_("Task UUID: %s.") % task["uuid"])
        else:
            print(_("ERROR: Invalid file name passed: %s"
                    ) % task_file,
                  file=sys.stderr)
            return 1
