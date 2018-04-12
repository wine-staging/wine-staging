#!/usr/bin/python2
# -*- coding: utf-8 -*-
#
# Automatic patch dependency checker and apply script generator.
#
# Copyright (C) 2014-2017 Sebastian Lackner
# Copyright (C) 2015 Michael Müller
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301, USA
#

from patchutils import escape_sh, escape_c
import argparse
import binascii
import cPickle as pickle
import contextlib
import fnmatch
import hashlib
import itertools
import math
import multiprocessing.pool
import operator
import os
import patchutils
import progressbar
import re
import signal
import subprocess
import sys
import tempfile
import textwrap
import xmlrpclib
import ConfigParser

_devnull = open(os.devnull, 'wb')

# Cached information to speed up patch dependency checks
upstream_commit = None

class config(object):
    path_cache              = ".patchupdate.cache"
    path_config             = os.path.expanduser("~/.config/patchupdate.conf")

    path_patches            = "patches"
    path_version            = "staging/VERSION"
    path_wine               = "staging/wine"

    path_template_script    = "staging/patchinstall.sh.in"
    path_script             = "patches/patchinstall.sh"

    path_IfDefined          = "9999-IfDefined.patch"

    bugtracker_url          = "https://bugs.winehq.org/xmlrpc.cgi"
    bugtracker_defaultcc    = ["michael@fds-team.de", "sebastian@fds-team.de",
                               "erich.e.hoover@wine-staging.com", "dmitry@baikal.ru"]
    bugtracker_user         = None
    bugtracker_pass         = None

    github_url              = "https://github.com/wine-staging/wine-staging"

class PatchUpdaterError(RuntimeError):
    """Failed to update patches."""
    pass

class PatchSet(object):
    def __init__(self, name, directory):
        self.name           = name
        self.variable       = None
        self.directory      = directory
        self.config         = []
        self.fixes          = []
        self.changes        = []
        self.disabled       = False
        self.ifdefined      = None

        self.files          = []
        self.patches        = []
        self.modified_files = set()
        self.depends        = set()
        self.auto_depends   = set()

        self.verify_time    = None

def _pairs(a):
    """Iterate over all pairs of elements contained in the list a."""
    for i, j in enumerate(a):
        for k in a[i+1:]:
            yield (j, k)

def _unique(iterable, key=None):
    "List unique elements, preserving order. Remember only the element just seen."
    # _unique('AAAABBBCCDAABBB') --> A B C D A B
    # _unique('ABBCcAD', str.lower) --> A B C A D
    return itertools.imap(next, itertools.imap(operator.itemgetter(1), itertools.groupby(iterable, key)))

def _binomial(n, k):
    "Compute binomial coefficient."
    num = 1
    div = 1
    for t in xrange(1, min(k, n - k) + 1):
        num *= n
        div *= t
        n -= 1
    return num // div

def _split_seq(iterable, size):
    """Split an iterator into chunks of a given size."""
    it = iter(iterable)
    items = list(itertools.islice(it, size))
    while items:
        yield items
        items = list(itertools.islice(it, size))

def _load_dict(filename):
    """Load a Python dictionary object from a file."""
    try:
        with open(filename) as fp:
            return pickle.load(fp)
    except IOError:
        return {}

def _save_dict(filename, value):
    """Save a Python dictionary object to a file."""
    with open("%s.new" % filename, "wb") as fp:
        pickle.dump(value, fp, pickle.HIGHEST_PROTOCOL)
    os.rename("%s.new" % filename, filename)

def _sha256(fp):
    """Calculate sha256sum from a file descriptor."""
    m = hashlib.sha256()
    fp.seek(0)
    while True:
        buf = fp.read(16384)
        if buf == "": break
        m.update(buf)
    return m.digest()

def _parse_int(val, default=0):
    """Parse an integer or boolean value."""
    if re.match("^[0-9]+$", val):
        return int(val)
    try:
        return {'true': 1, 'yes': 1, 'false': 0, 'no': 0}[val.lower()]
    except KeyError:
        return default

def _staging_version():
    """Get the current version number of Wine Staging."""
    with open(config.path_version) as fp:
        return fp.read().strip()

def _upstream_commit(commit=None):
    """Get latest wine commit."""
    if not os.path.isdir(config.path_wine):
        raise PatchUpdaterError("Please create a symlink to the wine repository in %s" % config.path_wine)
    if commit is None:
        commit = subprocess.check_output(["git", "rev-parse", "origin/master"], cwd=config.path_wine).strip()
    assert len(commit) == 40 and commit == commit.lower()
    return commit

def enum_patchsets(path):
    """Return a sorted list of all subdirectories of path."""
    dirs = []
    for name in os.listdir(path):
        directory = os.path.join(path, name)
        if not os.path.isdir(directory):
            continue
        dirs.append((name, directory))
    return sorted(dirs)

def load_patchsets():
    """Read information about all patchsets."""
    unique_id   = itertools.count()
    all_patches = {}
    name_to_id  = {}

    for name, directory in enum_patchsets(config.path_patches):
        patch = PatchSet(name, directory)

        # Load the definition file
        try:
            with open(os.path.join(directory, "definition")) as fp:
                for line in fp:
                    if line.startswith("#"):
                        continue
                    tmp = line.split(":", 1)
                    if len(tmp) != 2:
                        continue
                    patch.config.append((tmp[0].lower(), tmp[1].strip()))
        except IOError:
            pass

        # Enumerate .patch files in the given directory, enumerate individual patches and affected files
        for f in sorted(os.listdir(directory)):
            if not re.match("^[0-9]{4}-.*\\.patch$", f):
                continue
            if f.startswith(config.path_IfDefined):
                continue
            if ("exclude", f) in patch.config:
                continue
            if not os.path.isfile(os.path.join(directory, f)):
                continue
            patch.files.append(f)
            for p in patchutils.read_patch(os.path.join(directory, f)):
                patch.modified_files.add(p.modified_file)
                patch.patches.append(p)

        # No single patch within this directory, ignore it
        if len(patch.patches) == 0:
            print "WARNING: No patches found in directory %s" % (directory)
            del patch
            continue

        i = next(unique_id)
        all_patches[i]   = patch
        name_to_id[name] = i

    # Now read the definition files in a second step
    for i, patch in all_patches.iteritems():
        for key, val in patch.config:
            if key == "depends":
                if not name_to_id.has_key(val):
                    raise PatchUpdaterError("Definition file for %s references unknown dependency %s" % (patch.name, val))
                patch.depends.add(name_to_id[val])

            elif key == "apply-after":
                for j, other_patch in all_patches.iteritems():
                    if i != j and any([fnmatch.fnmatch(f, val) for f in other_patch.modified_files]):
                        patch.auto_depends.add(j)

            elif key == "apply-before":
                for j, other_patch in all_patches.iteritems():
                    if i != j and any([fnmatch.fnmatch(f, val) for f in other_patch.modified_files]):
                        other_patch.auto_depends.add(i)

            elif key == "fixes":
                r = re.match("^\\[ *(!)? *([0-9]+) *\\](.*)$", val)
                if r:
                    sync  = (r.group(1) != "!")
                    bugid = int(r.group(2))
                    patch.fixes.append((sync, bugid, r.group(3).strip()))
                    continue
                patch.fixes.append((False, None, val))

            elif key == "disabled":
                patch.disabled = _parse_int(val)

            elif key == "exclude":
                pass # Already processed above

            elif key == "ifdefined":
                patch.ifdefined = val

            else:
                print "WARNING: Ignoring unknown command in definition file for %s: %s" % (patch.name, line)

    # Filter autodepends on disabled patchsets
    for i, patch in all_patches.iteritems():
        patch.auto_depends = set([j for j in patch.auto_depends if not all_patches[j].disabled])

    return all_patches

def causal_time_combine(a, b):
    """Combines two timestamps into a new one."""
    return [max(a, b) for a, b in zip(a, b)]

def causal_time_smaller(a, b):
    """Checks if timestamp a is smaller than timestamp b."""
    return all([i <= j for i, j in zip(a,b)]) and any([i < j for i, j in zip(a,b)])

def causal_time_relation(all_patches, indices):
    """Checks if the dependencies of patches are compatible with a specific apply order."""
    for i, j in _pairs(indices):
        if causal_time_smaller(all_patches[j].verify_time, all_patches[i].verify_time):
            return False
    return True

def causal_time_relation_any(all_patches, indices):
    """Similar to causal_time_relation(), but also check all possible permutations of indices."""
    for i, j in _pairs(indices):
        if not (causal_time_smaller(all_patches[i].verify_time, all_patches[j].verify_time) or \
                causal_time_smaller(all_patches[j].verify_time, all_patches[i].verify_time)):
            return False
    return True

def contains_binary_patch(all_patches, indices, filename):
    """Checks if any patch with given indices affecting filename is a binary patch."""
    for i in indices:
        for patch in all_patches[i].patches:
            if patch.modified_file == filename and patch.is_binary:
                return True
    return False

def get_wine_file(filename):
    """Return the content of a file."""
    entry  = "%s:%s" % (upstream_commit, filename)
    result = tempfile.NamedTemporaryFile()
    try:
        content = subprocess.check_call(["git", "show", entry], cwd=config.path_wine, \
                                        stdout=result, stderr=_devnull)
    except subprocess.CalledProcessError as e:
        if e.returncode != 128: raise
    result.flush() # shouldn't be necessary because the subprocess writes directly to the fd
    return result

def extract_patch(patchset, filename):
    """Extract all changes to a specific file from a patchset."""
    p = tempfile.NamedTemporaryFile()
    m = hashlib.sha256()

    for patch in patchset.patches:
        if patch.modified_file != filename:
            continue
        assert not patch.is_binary
        for chunk in patch.read_chunks():
            p.write(chunk)
            m.update(chunk)
        p.write("\n")
        m.update("\n")

    p.flush()
    return (m.digest(), p)

def select_patches(all_patches, indices, filename):
    """Create a temporary patch file for each patchset and calculate the checksum."""
    selected_patches = {}
    for i in indices:
        selected_patches[i] = extract_patch(all_patches[i], filename)
    return selected_patches

def resolve_dependencies(all_patches, index = None, depends = None, auto_deps = True):
    """Returns a sorted list with all dependencies for a given patch."""

    def _resolve(depends):
        for i in sorted(depends):
            if all_patches[i].disabled: # Check for disabled patch
                raise PatchUpdaterError("Encountered dependency on disabled patchset %s" % all_patches[i].name)
            if all_patches[i].verify_resolved > 0: # Dependencies already resolved
                continue
            if all_patches[i].verify_resolved < 0: # Detect circular dependency
                raise PatchUpdaterError("Circular dependency while trying to resolve %s" % all_patches[i].name)

            # Recusively resolve dependencies
            all_patches[i].verify_resolved = -1
            _resolve(all_patches[i].depends)
            if auto_deps: _resolve(all_patches[i].auto_depends)
            all_patches[i].verify_resolved = 1
            resolved.append(i)

    for _, patch in all_patches.iteritems():
        patch.verify_resolved = 0

    resolved = []
    if depends is None:
        _resolve(all_patches[index].depends)
        if auto_deps: _resolve(all_patches[index].auto_depends)
    else:
        _resolve(depends)
    return resolved

def sync_bug_status(bugtracker, bug, url):
    """Automatically updates the STAGED information of a referenced bug."""

    # We don't want to reopen bugs
    if bug['status'] not in ["UNCONFIRMED", "NEW", "ASSIGNED", "REOPENED", "STAGED"]:
        return

    if config.bugtracker_user is None or config.bugtracker_pass is None:
        raise PatchUpdaterError("Can't update bug without username/password set")

    changes = { 'ids'               : bug['id'],
                'Bugzilla_login'    : config.bugtracker_user,
                'Bugzilla_password' : config.bugtracker_pass }

    # Update bug status
    if bug['status'] != "STAGED":
        changes['status'] = "STAGED"

    # Update patchset URL
    if bug['cf_staged_patchset'] != url:
        changes['cf_staged_patchset'] = url

    # Add missing CC contacts
    missing_cc = []
    for cc in config.bugtracker_defaultcc:
        if cc not in bug['cc']:
            missing_cc.append(cc)
    if len(missing_cc):
        changes["cc"] = {"add" : missing_cc}

    bugtracker.Bug.update(changes)

def check_bug_status(all_patches, sync_bugs=False):
    """Checks the information in the referenced bugs and corrects them if sync_bugs is set."""

    all_bugids = set()
    url_map = {}

    for _, patch in all_patches.iteritems():
        url = "%s/tree/master/%s" % (config.github_url, patch.directory)
        for sync, bugid, bugname in patch.fixes:
            if sync and bugid is not None:
                url_map[bugid] = url
                all_bugids.add(bugid)

    bugtracker  = xmlrpclib.ServerProxy(config.bugtracker_url)
    bug_list    = bugtracker.Bug.get(dict(ids=list(all_bugids)))
    staged_bugs = bugtracker.Bug.search(dict(status="STAGED"))

    once = True
    for bug in bug_list['bugs']:
        if bug['status'] != "STAGED":
            if once:
                print ""
                print "WARNING: The following bugs might require attention:"
                print ""
                once = False
            print " #%d - \"%s\" - %s %s - %s" % (bug['id'], bug['summary'], bug['status'],
                                                  bug['resolution'], bug['cf_staged_patchset'])
            if sync_bugs:
                sync_bug_status(bugtracker, bug, url_map[bug['id']])
        if bug['status'] == 'STAGED' and \
           bug['cf_staged_patchset'] != url_map[bug['id']] and \
           bug['cf_staged_patchset'] != url_map[bug['id']].replace('github.com/wine-staging','github.com/wine-compholio'):
            print 'Invalid staged patchset: #%d - \"%s\" - %s' %(bug['id'], bug['summary'], bug['cf_staged_patchset'])

    once = True
    for bug in staged_bugs['bugs']:
        if bug['id'] not in all_bugids:
            if once:
                print ""
                print "WARNING: The following bugs are incorrectly marked as STAGED:"
                print ""
                once = False
            print " #%d - \"%s\" - %s %s" % (bug['id'], bug['summary'], bug['status'],
                                             bug['resolution'])

    print ""

def generate_ifdefined(all_patches, skip_checks=False):
    """Update autogenerated ifdefined patches, which can be used to selectively disable features at compile time."""
    for i, patch in all_patches.iteritems():
        if patch.ifdefined is None:
            continue
        if patch.disabled:
            continue

        filename = os.path.join(patch.directory, config.path_IfDefined)
        headers = { 'author': "Wine Staging Team",
                    'email': "webmaster@fds-team.de",
                    'subject': "Autogenerated #ifdef patch for %s." % patch.name }

        if skip_checks:
            patch.files = [os.path.basename(filename)]
            continue

        with open(filename, "wb") as fp:
            fp.write("From: %s <%s>\n" % (headers['author'], headers['email']))
            fp.write("Subject: %s\n" % headers['subject'])
            fp.write("\n")
            fp.write("Based on patches by:\n")
            for author, email in sorted(set([(p.patch_author, p.patch_email) for p in patch.patches])):
                fp.write("    %s <%s>\n" % (author, email))
            fp.write("\n")

            depends = resolve_dependencies(all_patches, i)
            for f in sorted(patch.modified_files):

                # Reconstruct the state after applying the dependencies
                original = get_wine_file(f)
                selected_patches = select_patches(all_patches, depends, f)
                failed = []

                try:
                    for j in depends:
                        failed.append(j)
                        original = patchutils.apply_patch(original, selected_patches[j][1], fuzz=0)
                except patchutils.PatchApplyError:
                    raise PatchUpdaterError("Changes to file %s don't apply: %s" %
                                            (f, ", ".join([all_patches[j].name for j in failed])))

                # Now apply the main patch
                p = extract_patch(patch, f)[1]

                try:
                    failed.append(i)
                    patched = patchutils.apply_patch(original, p, fuzz=0)
                except patchutils.PatchApplyError:
                    raise PatchUpdaterError("Changes to file %s don't apply: %s" %
                                            (f, ", ".join([all_patches[j].name for j in failed])))

                # Now get the diff between both
                diff = patchutils.generate_ifdef_patch(original, patched, ifdef=patch.ifdefined)
                if diff is not None:
                    fp.write("diff --git a/%s b/%s\n" % (f, f))
                    fp.write("--- a/%s\n" % f)
                    fp.write("+++ b/%s\n" % f)
                    while True:
                        buf = diff.read(16384)
                        if buf == "": break
                        fp.write(buf)
                    diff.close()

            # Close the file
            fp.close()

        # Add changes to git
        subprocess.call(["git", "add", filename])

        # Add the autogenerated file as a last patch
        patch.files = [os.path.basename(filename)]
        for p in patch.patches:
            p.filename      = None
            p.modified_file = None
        for p in patchutils.read_patch(filename):
            assert p.modified_file in patch.modified_files
            p.patch_author  = None
            patch.patches.append(p)

def generate_apply_order(all_patches, skip_checks=False):
    """Resolve dependencies, and afterwards check if everything applies properly."""
    depends     = sorted([i for i, patch in all_patches.iteritems() if not patch.disabled])
    resolved    = resolve_dependencies(all_patches, depends=depends)
    max_patches = max(resolved) + 1

    if skip_checks:
        return resolved

    # Generate timestamps based on dependencies, still required for binary patches
    # Find out which files are modified by multiple patches
    modified_files = {}
    for i, patch in [(i, all_patches[i]) for i in resolved]:
        patch.verify_time = [0]*max_patches
        patch.verify_time[i] += 1
        for j in patch.depends:
            patch.verify_time = causal_time_combine(patch.verify_time, all_patches[j].verify_time)

        for f in patch.modified_files:
            if f not in modified_files:
                modified_files[f] = []
            modified_files[f].append(i)

    # Check dependencies
    dependency_cache = _load_dict(config.path_cache)
    pool = multiprocessing.pool.ThreadPool(processes=4)
    try:
        for filename, indices in modified_files.iteritems():

            # If one of patches is a binary patch, then we cannot / won't verify it - require dependencies in this case
            if contains_binary_patch(all_patches, indices, filename):
                if not causal_time_relation_any(all_patches, indices):
                    raise PatchUpdaterError("Because of binary patch modifying file %s the following patches need explicit dependencies: %s" %
                                            (filename, ", ".join([all_patches[i].name for i in indices])))
                continue

            original_content = get_wine_file(filename)
            original_hash    = _sha256(original_content)
            selected_patches = select_patches(all_patches, indices, filename)

            # Generate a unique id based on the original content, the selected patches
            # and the dependency information. Since this information only has to be compared
            # we can throw it into a single hash.
            m = hashlib.sha256()
            m.update(original_hash)
            for i in indices:
                m.update("P%s" % selected_patches[i][0])
                for j in indices:
                    if causal_time_smaller(all_patches[j].verify_time, all_patches[i].verify_time):
                        m.update("D%s" % selected_patches[j][0])
            unique_hash = m.digest()

            # Skip checks if it matches the information from the cache
            # For backwards compatibility, convert string entries to list
            if dependency_cache.has_key(filename):
                if not isinstance(dependency_cache[filename], list):
                    dependency_cache[filename] = [dependency_cache[filename]]
                if unique_hash in dependency_cache[filename]:
                    dependency_cache[filename].append(unique_hash)
                    dependency_cache[filename].remove(unique_hash)
                    continue

            chunk_size = 20
            iterables = []
            total = 0
            for i in xrange(1, len(indices) + 1):
                # HACK: It is no longer feasible to check all combinations for configure.ac.
                # Only check corner cases (applying individual patches and applying all patches).
                if filename == "configure.ac" and i > 4 and i <= len(indices) - 4: continue
                iterables.append(itertools.combinations(indices, i))
                total += _binomial(len(indices), i)

            # Show a progress bar while applying the patches - this task might take some time
            with progressbar.ProgressBar(desc=filename, total=total / chunk_size) as progress:

                def test_apply(current):
                    set_apply = [(i, all_patches[i]) for i in current]
                    set_skip  = [(i, all_patches[i]) for i in indices if i not in current]

                    # Check if there is any patch2 which depends directly or indirectly on patch1.
                    # If this is the case we found an impossible situation, we can be skipped in this test.
                    for i, patch1 in set_apply:
                        for j, patch2 in set_skip:
                            if causal_time_smaller(patch2.verify_time, patch1.verify_time):
                                return True # we can skip this test

                    try:
                        original = original_content
                        for i, _ in set_apply:
                            original = patchutils.apply_patch(original, selected_patches[i][1], fuzz=0)
                    except patchutils.PatchApplyError:
                        return False

                    return True # everything is fine

                def test_apply_seq(current_list):
                    for current in current_list:
                        if not test_apply(current):
                            return current
                    return None

                it = _split_seq(itertools.chain(*iterables), chunk_size)
                for k, failed in enumerate(pool.imap_unordered(test_apply_seq, it)):
                    if failed is not None:
                        progress.finish("<failed to apply>")
                        raise PatchUpdaterError("Changes to file %s don't apply: %s" %
                                                (filename, ", ".join([all_patches[i].name for i in failed])))
                    progress.update(k)

            # Update the dependency cache, store max 10 entries per file
            if not dependency_cache.has_key(filename):
                dependency_cache[filename] = []
            dependency_cache[filename].append(unique_hash)
            dependency_cache[filename] = dependency_cache[filename][-10:]

        # Delete outdated cache information
        for filename in dependency_cache.keys():
            if not modified_files.has_key(filename):
                del dependency_cache[filename]
    finally:
        pool.close()
        _save_dict(config.path_cache, dependency_cache)

    return resolved

def generate_script(all_patches, resolved):
    """Generate script to apply patches."""

    # Generate code for helper functions
    lines = []
    lines.append("# Enable or disable all patchsets\n")
    lines.append("patch_enable_all ()\n")
    lines.append("{\n")
    for i, patch in sorted([(i, all_patches[i]) for i in resolved], key=lambda x:x[1].name):
        patch.variable = "enable_%s" % patch.name.replace("-","_").replace(".","_")
        lines.append("\t%s=\"$1\"\n" % patch.variable)
    lines.append("}\n")
    lines.append("\n")

    lines.append("# Enable or disable a specific patchset\n")
    lines.append("patch_enable ()\n")
    lines.append("{\n")
    lines.append("\tcase \"$1\" in\n")
    for i, patch in sorted([(i, all_patches[i]) for i in resolved], key=lambda x:x[1].name):
        lines.append("\t\t%s)\n" % patch.name)
        lines.append("\t\t\t%s=\"$2\"\n" % patch.variable)
        lines.append("\t\t\t;;\n")
    lines.append("\t\t*)\n")
    lines.append("\t\t\treturn 1\n")
    lines.append("\t\t\t;;\n")
    lines.append("\tesac\n")
    lines.append("\treturn 0\n")
    lines.append("}\n")
    lines_helpers = lines

    # Generate code for dependency resolver
    lines = []
    for i, patch in [(i, all_patches[i]) for i in reversed(resolved)]:
        if len(patch.depends):
            lines.append("if test \"$%s\" -eq 1; then\n" % patch.variable)
            for j in sorted(patch.depends):
                lines.append("\tif test \"$%s\" -gt 1; then\n" % all_patches[j].variable)
                lines.append("\t\tabort \"Patchset %s disabled, but %s depends on that.\"\n" %
                             (all_patches[j].name, patch.name))
                lines.append("\tfi\n")
            for j in sorted(patch.depends):
                lines.append("\t%s=1\n" % all_patches[j].variable)
            lines.append("fi\n\n")
    lines_resolver = lines

    # Generate code for applying all patchsets
    lines = []
    for i, patch in [(i, all_patches[i]) for i in resolved]:
        lines.append("# Patchset %s\n" % patch.name)
        lines.append("# |\n")

        # List dependencies (if any)
        if len(patch.depends):
            depends = resolve_dependencies(all_patches, i, auto_deps=False)
            lines.append("# | This patchset has the following (direct or indirect) dependencies:\n")
            lines.append("# |   *\t%s\n" % "\n# | \t".join(textwrap.wrap(
                ", ".join([all_patches[j].name for j in depends]), 120)))
            lines.append("# |\n")

        # List all bugs fixed by this patchset
        if any([bugid is not None for sync, bugid, bugname in patch.fixes]):
            lines.append("# | This patchset fixes the following Wine bugs:\n")
            for sync, bugid, bugname in patch.fixes:
                if bugid is not None:
                    lines.append("# |   *\t%s\n" % "\n# | \t".join(textwrap.wrap("[#%d] %s" % (bugid, bugname), 120)))
            lines.append("# |\n")

        # List all modified files
        lines.append("# | Modified files:\n")
        lines.append("# |   *\t%s\n" % "\n# | \t".join(textwrap.wrap(", ".join(sorted(patch.modified_files)), 120)))
        lines.append("# |\n")
        lines.append("if test \"$%s\" -eq 1; then\n" % patch.variable)
        for f in patch.files:
            lines.append("\tpatch_apply %s\n" % os.path.join(patch.name, f))
        if len(patch.patches):
            lines.append("\t(\n")
            for p in _unique(patch.patches, key=lambda p: (p.patch_author, p.patch_subject, p.patch_revision)):
                if p.patch_author is None: continue
                lines.append("\t\tprintf '%%s\\n' '+    { \"%s\", \"%s\", %d },';\n" %
                             (escape_sh(escape_c(p.patch_author)), escape_sh(escape_c(p.patch_subject)), p.patch_revision))
            lines.append("\t) >> \"$patchlist\"\n")
        lines.append("fi\n\n")
    lines_apply = lines

    with open(config.path_template_script) as template_fp:
        template = template_fp.read()
    with open(config.path_script, "w") as fp:
        fp.write(template.format(staging_version=_staging_version(),
                                 upstream_commit=upstream_commit,
                                 patch_helpers="".join(lines_helpers).rstrip("\n"),
                                 patch_resolver="".join(lines_resolver).rstrip("\n"),
                                 patch_apply="".join(lines_apply).rstrip("\n")))

    # Add changes to git
    subprocess.call(["git", "add", config.path_script])


if __name__ == "__main__":

    # Hack to avoid KeyboardInterrupts on different threads
    def _sig_int(signum=None, frame=None):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        raise RuntimeError("CTRL+C pressed")
    signal.signal(signal.SIGINT, _sig_int)

    def _check_commit_hash(commit):
        if len(commit) != 40 or commit != commit.lower():
            raise argparse.ArgumentTypeError("not a valid commit hash")
        return commit

    parser = argparse.ArgumentParser(description="Automatic patch dependency checker and apply script generator.")
    parser.add_argument('--skip-checks', action='store_true', help="Skip dependency checks")
    parser.add_argument('--commit', type=_check_commit_hash, help="Use given commit hash instead of HEAD")
    parser.add_argument('--sync-bugs', action='store_true', help="Update bugs in bugtracker (requires admin rights)")
    parser.add_argument('--skip-bugs', action='store_true', help="Skip bugtracker checks")
    args = parser.parse_args()

    tools_directory = os.path.dirname(os.path.realpath(__file__))
    os.chdir(os.path.join(tools_directory, "./.."))

    config_parser = ConfigParser.ConfigParser()
    config_parser.read(config.path_config)

    try:
        config.bugtracker_user = config_parser.get('bugtracker', 'username')
        config.bugtracker_pass = config_parser.get('bugtracker', 'password')
    except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
        config.bugtracker_user = None
        config.bugtracker_pass = None

    try:
        upstream_commit = _upstream_commit(args.commit)
        all_patches = load_patchsets()

        # Check bugzilla
        if not args.skip_bugs:
            check_bug_status(all_patches, sync_bugs=args.sync_bugs)

        # Update autogenerated files
        generate_ifdefined(all_patches, skip_checks=args.skip_checks)
        resolved = generate_apply_order(all_patches, skip_checks=args.skip_checks)
        generate_script(all_patches, resolved)

    except PatchUpdaterError as e:
        print ""
        print "ERROR: %s" % e
        print ""
        exit(1)
