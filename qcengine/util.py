"""
Several import utilities
"""

import abc
import importlib
import io
import json
import operator
import subprocess
import sys
import time
import traceback
from contextlib import contextmanager
from typing import Any, Dict

from pydantic import BaseModel
from qcelemental.models import ComputeError, FailedOperation

from . import config

__all__ = ["compute_wrapper", "get_module_function", "model_wrapper", "handle_output_metadata"]


def model_wrapper(input_data, model):
    """
    Wrap input data in the given model, or return a controlled error
    """
    try:
        if isinstance(input_data, dict):
            input_data = model(**input_data)
    except Exception:
        failure = FailedOperation(
            input_data=input_data,
            success=False,
            error=ComputeError(
                error_type="input_error",
                error_message=("Input data could not be processed correctly:\n" + traceback.format_exc())))
        return failure
    return input_data


@contextmanager
def compute_wrapper(capture_output=True):
    """Wraps compute for timing, output capturing, and raise protection
    """

    ret = {"stdout": "", "stderr": ""}

    # Start timer
    comp_time = time.time()

    # Capture stdout/err
    new_stdout = io.StringIO()
    new_stderr = io.StringIO()
    if capture_output:

        old_stdout, sys.stdout = sys.stdout, new_stdout
        old_stderr, sys.stderr = sys.stderr, new_stderr

    try:
        yield ret
        ret["success"] = True
    except Exception as e:
        ret["error_message"] = "QCEngine Call Error:\n" + traceback.format_exc()
        ret["success"] = False

    # Place data
    ret["wall_time"] = time.time() - comp_time
    if capture_output:
        ret["stdout"] = new_stdout.getvalue()
        ret["stderr"] = new_stderr.getvalue()

        if ret["stdout"] == "":
            ret["stdout"] = "No stdout recieved."

        if ret["stderr"] == "":
            ret["stderr"] = "No stderr recieved."

        # Replace stdout/err
        if capture_output:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
    else:
        ret["stdout"] = "stdout not captured"
        ret["stderr"] = "stderr not captured"


def get_module_function(module, func_name, subpackage=None):
    """Obtains a function from a given string

    Parameters
    ----------
    module : str
        The module to pull the function from
    func_name : str
        The name of the function to aquire, can be in a subpackage
    subpackage : None, optional
        Explicitly import a subpackage if required

    Returns
    -------
    ret : function
        The requested functions

    Example
    -------

    # Import numpy.linalg.eigh
    f = get_module_function("numpy", "linalg.eigh")
    f(np.ones((2, 2)))

    """
    # Will throw import error if we fail
    pkg = importlib.import_module(module, subpackage)

    return operator.attrgetter(func_name)(pkg)


def handle_output_metadata(output_data, metadata, raise_error=False, return_dict=True):
    """
    Fuses general metadata and output together.

    Returns
    -------
    result : dict or pydantic.models.Result
        Output type depends on return_dict or a dict if an error was generated in model construction
    """

    if isinstance(output_data, dict):
        output_fusion = output_data  # Error handling
    else:
        output_fusion = output_data.dict()

    # Do not override if computer generates
    output_fusion["stdout"] = output_fusion.pop("stdout", metadata["stdout"])
    output_fusion["stderr"] = output_fusion.pop("stderr", metadata["stderr"])

    if metadata["success"] is not True:
        output_fusion["success"] = False
        output_fusion["error"] = {"error_type": "meta_error", "error_message": metadata["error_message"]}

    # Raise an error if one exists and a user requested a raise
    if raise_error and (output_fusion["success"] is not True):
        msg = "stdout:\n" + output_fusion["stdout"]
        msg += "\nstderr:\n" + output_fusion["stderr"]
        print(msg)
        raise ValueError(output_fusion["error"]["error_message"])

    # Fill out provenance datadata
    provenance_augments = config.get_provenance_augments()
    provenance_augments["wall_time"] = metadata["wall_time"]
    if "provenance" in output_fusion:
        output_fusion["provenance"].update(provenance_augments)
    else:
        # Add onto the augments with some missing info
        provenance_augments["creator"] = "QCEngine"
        provenance_augments["version"] = provenance_augments["qcengine_version"]
        output_fusion["provenance"] = provenance_augments

    # We need to return the correct objects; e.g. Results, Procedures
    if output_fusion["success"]:
        # This will only execute if everything went well
        ret = output_data.__class__(**output_fusion)
    else:
        # Should only be reachable on failures
        ret = FailedOperation(
            success=output_fusion.pop("success", False), error=output_fusion.pop("error"), input_data=output_fusion)
    if return_dict:
        return json.loads(ret.json())  # Use Pydantic to serialize, then reconstruct as Python dict of Python Primals
    else:
        return ret


def terminate_process(proc, timeout: int=15):
    if proc.poll() is None:

        # Sigint (keyboard interupt)
        if sys.platform.startswith('win'):
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.send_signal(signal.SIGINT)

        try:
            start = time.time()
            while (proc.poll() is None) and (time.time() < (start + timeout)):
                time.sleep(0.02)

        # Flat kill
        finally:
            proc.kill()


@contextmanager
def popen(args, **kwargs):
    """
    Opens a background task

    Code and idea from dask.distributed's testing suite
    https://github.com/dask/distributed
    """
    args = list(args)

    # Bin prefix
    if sys.platform.startswith('win'):
        bin_prefix = os.path.join(sys.prefix, 'Scripts')
    else:
        bin_prefix = os.path.join(sys.prefix, 'bin')

    # Do we prefix with Python?
    if kwargs.pop("append_prefix", False):
        args[0] = os.path.join(bin_prefix, args[0])

    if sys.platform.startswith('win'):
        # Allow using CTRL_C_EVENT / CTRL_BREAK_EVENT
        kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP

    kwargs['stdout'] = subprocess.PIPE
    kwargs['stderr'] = subprocess.PIPE
    proc = subprocess.Popen(args, **kwargs)
    try:
        yield proc
    except Exception:
        dump_stdout = True
        raise

    finally:
        try:
            terminate_process(proc)
        finally:
            output, error = proc.communicate()
            if dump_stdout:
                print('\n' + '-' * 30)
                print("\n|| Process command: {}".format(" ".join(args)))
                print('\n|| Process stderr: \n{}'.format(error.decode()))
                print('-' * 30)
                print('\n|| Process stdout: \n{}'.format(output.decode()))
                print('-' * 30)


class ProgramExecutor(BaseModel, abc.ABC):

    requires_folder: bool
    requires_scratch: bool
    single_node: bool
    max_cores: Optional[int]
    max_memory: Optional[float]

    @abc.abstractmethod
    def build_input(self):
        pass

    @abc.abstractmethod
    def parse_output(self):
        pass

    def execute(args, **kwargs):
        """
        Runs a process in the background until complete.

        Returns True if exit code zero
        """

        timeout = kwargs.pop("timeout", 30)
        terminate_after = kwargs.pop("interupt_after", None)
        with popen(args, **kwargs) as proc:
            if terminate_after is None:
                proc.wait(timeout=timeout)
            else:
                time.sleep(terminate_after)
                terminate_process(proc)

            retcode = proc.poll()

        return retcode == 0
