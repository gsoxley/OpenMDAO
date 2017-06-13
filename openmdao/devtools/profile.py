from __future__ import print_function

import os
import sys
import ast
from time import time as etime
from inspect import getmembers
from fnmatch import fnmatchcase
import argparse
import json
import atexit
from collections import defaultdict
from itertools import chain

from six import iteritems

try:
    from mpi4py import MPI
except ImportError:
    MPI = None

from openmdao.devtools.webview import webview
from openmdao.devtools.trace import func_group


def _prof_node(parts, obj=None):
    name = '@'.join(parts)
    return {
        'name': name,
        'short_name': parts[-1],
        'time': 0.,
        'count': 0,
        'tot_time': 0.,
        'tot_count': 0,
        'pct_total': 0.,
        'tot_pct_total': 0.,
        'pct_parent': 0.,
        'child_time': 0.,
        'obj': obj,
    }

_profile_methods = None
_profile_prefix = None
_profile_out = None
_profile_start = None
_profile_setup = False
_profile_total = 0.0
_profile_matches = {}
_call_stack = []
_timing_stack = []
_inst_data = {}
_objs = {}   # mapping of ids to instance objects
_file2class = {}


def setup(prefix='iprof', methods=None, prof_dir=None, finalize=True):
    """
    Instruments certain important openmdao methods for profiling.

    Args
    ----

    prefix : str ('iprof')
        Prefix used for the raw profile data. Process rank will be appended
        to it to get the actual filename.  When not using MPI, rank=0.

    methods : dict, optional
        A dict of profiled methods to override the default set.  The key
        is the method name or glob pattern and the value is a tuple of class
        objects used for isinstance checking.  The default set of methods is:

        ::

            {
                "*": (System, Jacobian, Matrix, Solver, Driver, Problem),
            }

    prof_dir : str
        Directory where the profile files will be written. Defaults to the
        current directory.

    finallize : bool
        If True, register a function to finalize the profile before exit.

    """

    global _profile_prefix, _profile_methods, _profile_matches
    global _profile_setup, _profile_total, _profile_out, _file2class

    if _profile_setup:
        raise RuntimeError("profiling is already set up.")

    if prof_dir is None:
        _profile_prefix = os.path.join(os.getcwd(), prefix)
    else:
        _profile_prefix = os.path.join(os.path.abspath(prof_dir), prefix)

    _profile_setup = True

    if methods is None:
        _profile_methods = func_group('openmdao')
    else:
        _profile_methods = methods

    rank = MPI.COMM_WORLD.rank if MPI else 0
    _profile_out = open("%s.%d" % (_profile_prefix, rank), 'wb')

    atexit.register(_finalize_profile)

    _profile_matches, _file2class = _collect_methods(_profile_methods)


def _collect_methods(method_dict):
    """
    Iterate over a dict of method name patterns mapped to classes.  Search
    through the classes for anything that matches and return a dict of
    exact name matches and their correspoding classes.

    Parameters
    ----------
    method_dict : {pattern1: classes1, ... pattern_n: classes_n}
        Dict of glob patterns mapped to lists of classes used for isinstance checks

    Returns
    -------
    dict
        Dict of method names and tuples of all classes that matched for that method.
    """
    matches = {}
    file2class = defaultdict(list)  # map files to classes

    # TODO: update this to also work with stand-alone functions
    for pattern, classes in iteritems(method_dict):
        for class_ in classes:
            for name, obj in getmembers(class_):
                if callable(obj) and (pattern == '*' or fnmatchcase(name, pattern)):
                    if name in matches:
                        matches[name].append(class_)
                    else:
                        matches[name] = [class_]

    # convert values to tuples so we can use in isinstance call
    for name in matches:
        matches[name] = tuple(matches[name])

    return matches, file2class


# TODO: create a cython version of this to cut down on overhead...
def _instance_profile(frame, event, arg):
    """
    Collects profile data for functions that match _profile_matches.
    The data collected will include time elapsed, number of calls, ...
    """
    global _call_stack, _profile_out, _profile_struct, _inst_data, \
           _profile_funcs_dict, _profile_start, _profile_matches, _file2class

    if event == 'call':
        func_name = frame.f_code.co_name
        if func_name in _profile_matches:
            loc = frame.f_locals
            if 'self' in loc:
                self = loc['self']
                if isinstance(self, _profile_matches[func_name]):
                    name = "%s#%d#%d#%s" % (frame.f_code.co_filename,
                                              frame.f_code.co_firstlineno, id(self), func_name)
                    _call_stack.append(name)
                    _timing_stack.append(etime())

    elif event == 'return':
        func_name = frame.f_code.co_name
        if func_name in _profile_matches:
            loc = frame.f_locals
            if 'self' in loc:
                self = loc['self']
                if isinstance(self, _profile_matches[func_name]):
                    final = etime()
                    path = '@'.join(_call_stack)
                    if path not in _inst_data:
                        _inst_data[path] = _prof_node(_call_stack, self)

                    _call_stack.pop()

                    pdata = _inst_data[path]
                    pdata['time'] += final - _timing_stack.pop()
                    pdata['count'] += 1


def start():
    """
    Turn on profiling.
    """
    global _profile_start, _profile_setup, _call_stack, _inst_data
    if _profile_start is not None:
        print("profiling is already active.")
        return

    if not _profile_setup:
        setup()  # just do a default setup

    _profile_start = etime()
    _call_stack.append('$total')
    if '$total' not in _inst_data:
        _inst_data['$total'] = _prof_node(['$total'])

    if sys.getprofile() is not None:
        raise RuntimeError("another profile function is already active.")
    sys.setprofile(_instance_profile)


def stop():
    """
    Turn off profiling.
    """
    global _profile_total, _profile_start, _call_stack, _inst_data
    if _profile_start is None:
        return

    sys.setprofile(None)

    _call_stack.pop()

    _profile_total += (etime() - _profile_start)
    _inst_data['$total']['time'] = _profile_total
    _inst_data['$total']['count'] += 1
    _profile_start = None


class ClassVisitor(ast.NodeVisitor):
    def __init__(self, fname, cache):
        ast.NodeVisitor.__init__(self)
        self.fname = fname
        self.cache = cache
        self.class_stack = []

    def visit_ClassDef(self, node):
        self.class_stack.append(node.name)
        for bnode in node.body:
            self.visit(bnode)
        self.class_stack.pop()

    def visit_FunctionDef(self, node):
        if self.class_stack:
            qual =  (None, '.'.join(self.class_stack),  node.name)
        else:
            qual = ("<%s>" % self.fname, None, node.name)

        self.cache[node.lineno] = qual


def _find_qualified_name(filename, line, cache):
    """
    Determine full function name (class.method) or function for unbound functions.

    Parameters
    ----------
    filename : str
        Name of file containing source code.
    line : int
        Line number within the give file.
    cache : dict
        A dictionary containing infomation by filename.

    Returns
    -------
    str or None
        Fully qualified function/method name or None.
    """

    if filename not in cache:
        fcache = {}

        with open(filename, 'Ur') as f:
            contents = f.read()
            if len(contents) > 0 and contents[-1] != '\n':
                contents += '\n'

            ClassVisitor(filename, fcache).visit(ast.parse(contents, filename))

        cache[filename] = fcache

    return cache[filename][line]


def _finalize_profile():
    """
    Called at exit to write out the profiling data.
    """
    global _profile_prefix, _profile_funcs_dict, _profile_total, _inst_data

    stop()

    # fix names in _inst_data
    _obj_map = {}
    cache = {}
    idents = defaultdict(dict)  # map idents to a smaller number
    for funcpath, data in iteritems(_inst_data):
        parts = funcpath.rsplit('@', 1)
        fname = parts[-1]
        if fname == '$total':
            continue
        filename, line, ident, _ = fname.split('#')
        qfile, qclass, qname = _find_qualified_name(filename, int(line), cache)

        idict = idents[(qfile, qclass)]
        if ident in idict:
            ident = idict[ident]
        else:
            idict[ident] = len(idict)
            ident = idict[ident]

        try:
            name = data['obj'].pathname
        except AttributeError:
            if qfile is None:
                _obj_map[fname] = "<%s#%d.%s>" % (qclass, ident, qname)
            else:
                _obj_map[fname] = "<%s.%s>" % (qfile, qname)
        else:
            _obj_map[fname] = '.'.join((name, "<%s.%s>" % (qclass, qname)))

    _obj_map['$total'] = '$total'
    _obj_map['$parent'] = '$parent'

    # compute child times
    for funcpath, data in iteritems(_inst_data):
        parts = funcpath.rsplit('@', 1)
        if len(parts) > 1:
            _inst_data[parts[0]]['child_time'] += data['time']

    # in order to make the D3 partition layout give accurate proportions, we can only put values
    # into leaf nodes because the parent node values get overridden by the sum of the children. To
    # get around this, we create a child for each non-leaf node with the name '$parent' and put the
    # time exclusive to the parent into that child, so that when all of the children are summed, they'll
    # add up to the correct time for the parent and the visual proportions of the parent will be correct.

    # compute child timings
    parnodes = []
    for funcpath, node in iteritems(_inst_data):
        if node['child_time'] > 0.:
            parts = funcpath.split('@')
            pparts = parts + ['$parent']
            ex_child_node = _prof_node(pparts)
            ex_child_node['time'] = node['time'] - node['child_time']
            ex_child_node['count'] = 1

            parnodes.append(('@'.join(pparts), ex_child_node))

    rank = MPI.COMM_WORLD.rank if MPI else 0

    fname = os.path.basename(_profile_prefix)
    with open("%s.%d" % (fname, rank), 'w') as f:
        for name, data in chain(iteritems(_inst_data), parnodes):
            new_name = '@'.join([_obj_map[s] for s in name.split('@')])
            f.write("%s %d %f\n" % (new_name, data['count'], data['time']))


def _iter_raw_prof_file(rawname):
    """
    Returns an iterator of (funcpath, count, elapsed_time)
    from a raw profile data file.
    """
    with open(rawname, 'r') as f:
        for line in f:
            path, count, elapsed = line.split()
            yield path, int(count), float(elapsed)


def process_profile(flist):
    """
    Take the generated raw profile data, potentially from multiple files,
    and combine it to get execution counts and timing data.

    Args
    ----

    flist : list of str
        Names of raw profiling data files.

    """

    nfiles = len(flist)
    totals = {}

    tree_nodes = {}

    for fname in flist:
        ext = os.path.splitext(fname)[1]
        try:
            int(ext.lstrip('.'))
            dec = ext
        except:
            dec = False

        for funcpath, count, t in _iter_raw_prof_file(fname):

            parts = funcpath.split('@')

            # for multi-file MPI profiles, decorate names with the rank
            if nfiles > 1 and dec:
                parts = ["%s%s" % (p,dec) for p in parts]
                funcpath = '@'.join(parts)

            tree_nodes[funcpath] = node = _prof_node(parts)
            node['time'] += t
            node['count'] += count

            funcname = parts[-1]
            if funcname == '$parent':
                continue

            if funcname in totals:
                tnode = totals[funcname]
            else:
                totals[funcname] = tnode = _prof_node(parts)

            tnode['tot_time'] += t
            tnode['tot_count'] += count

    for funcpath, node in iteritems(tree_nodes):
        parts = funcpath.rsplit('@', 1)
        if parts[-1] != '$parent':
            node['tot_time'] = totals[parts[-1]]['tot_time']
            node['tot_count'] = totals[parts[-1]]['tot_count']
            node['pct_parent'] = node['time'] / tree_nodes[parts[0]]['time']
            node['pct_total'] = node['time'] / tree_nodes['$total']['time']
            node['tot_pct_total'] = totals[parts[-1]]['tot_time'] / tree_nodes['$total']['time']
        del node['obj']
        del node['child_time']

    tree_nodes['$total']['tot_time'] = tree_nodes['$total']['time']

    for funcpath, node in iteritems(tree_nodes):
        parts = funcpath.rsplit('@', 1)
        # D3 sums up all children to get parent value, so we need to
        # zero out the parent value else we get double the value we want
        # once we add in all of the times from descendants.
        if parts[-1] == '$parent':
            tree_nodes[parts[0]]['time'] = 0.

    return list(tree_nodes.values()), totals


def prof_dump(fname=None):
    """Print the contents of the given raw profile data file to stdout.

    Args
    ----

    fname : str
        Name of raw profile data file.
    """

    if fname is None:
        fname = sys.argv[1]

    for funcpath, count, t in _iter_raw_prof_file(fname):
        print(funcpath, count, t)


def prof_totals():
    """Called from the command line to create a file containing total elapsed
    times and number of calls for all profiled functions.

    """
    parser = argparse.ArgumentParser()
    parser.add_argument('-o', '--outfile', action='store', dest='outfile',
                        metavar='OUTFILE', default='sys.stdout',
                        help='Name of file containing function total counts and elapsed times.')
    parser.add_argument('rawfiles', metavar='rawfile', nargs='*',
                        help='File(s) containing raw profile data to be processed. Wildcards are allowed.')

    #TODO: add arg to set max number of results (starting at largest)

    options = parser.parse_args()

    if not options.rawfiles:
        print("No files to process.")
        sys.exit(0)

    if options.outfile == 'sys.stdout':
        out_stream = sys.stdout
    else:
        out_stream = open(options.outfile, 'w')

    _, totals = process_profile(options.rawfiles)

    total_time = totals['$total']['tot_time']

    try:

        out_stream.write("\nTotal     Total           Function\n")
        out_stream.write("Calls     Time (s)    %   Name\n")

        for func, data in sorted([(k,v) for k,v in iteritems(totals)],
                                    key=lambda x:x[1]['tot_time']):
            out_stream.write("%6d %11f %6.2f %s\n" %
                               (data['tot_count'], data['tot_time'], (data['tot_time']/total_time*100.), func))
    finally:
        if out_stream is not sys.stdout:
            out_stream.close()

def prof_view():
    """Called from a command line to generate an html viewer for profile data."""

    parser = argparse.ArgumentParser()
    parser.add_argument('--noshow', action='store_true', dest='noshow',
                        help="Don't pop up a browser to view the data.")
    parser.add_argument('-t', '--title', action='store', dest='title',
                        default='Profile of Method Calls by Instance',
                        help='Title to be displayed above profiling view.')
    parser.add_argument('rawfiles', metavar='rawfile', nargs='*',
                        help='File(s) containing raw profile data to be processed. Wildcards are allowed.')

    options = parser.parse_args()

    if not options.rawfiles:
        print("No files to process.")
        sys.exit(0)

    call_graph, _ = process_profile(options.rawfiles)

    viewer = "icicle.html"
    code_dir = os.path.dirname(os.path.abspath(__file__))

    with open(os.path.join(code_dir, viewer), "r") as f:
        template = f.read()

    graphjson = json.dumps(call_graph)

    outfile = 'profile_' + viewer
    with open(outfile, 'w') as f:
        template = template.replace('$call_graph_data', graphjson)
        f.write(template.replace('$title', options.title))

    if not options.noshow:
        webview(outfile)

def main():
    from optparse import OptionParser
    usage = "profile.py [scriptfile [arg] ..."
    parser = OptionParser(usage=usage)
    parser.allow_interspersed_args = False
    parser.add_option('-v', '--view', dest="view",
        help="View of profiling output, ['web', 'totals', 'dump']", default='web')

    if not sys.argv[1:]:
        parser.print_usage()
        sys.exit(2)

    (options, args) = parser.parse_args()
    sys.argv[:] = args

    if len(args) > 0:
        progname = args[0]
        sys.path.insert(0, os.path.dirname(progname))

        with open(progname, 'rb') as fp:
            code = compile(fp.read(), progname, 'exec')
        globs = {
            '__file__': progname,
            '__name__': '__main__',
            '__package__': None,
            '__cached__': None,
        }

        setup(finalize=False)
        sys.argv.append('iprof.0')
        start()
        exec (code, globs)
        _finalize_profile()

        if options.view == 'web':
            prof_view()
        elif options.view == 'console':
            prof_totals()
        elif options.view == 'dump':
            prof_dump()


if __name__ == '__main__':
    main()
