#!/usr/bin/env python2

# Copyright (C) 2020 Zebediah Figura
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

import email.header, getopt, os, subprocess, sys

stagingdir = os.path.dirname(os.path.realpath(__file__)) + '/../'
patchdir = stagingdir + 'patches'
if 'DESTDIR' in os.environ:
    winedir = os.environ['DESTDIR']
else:
    winedir = '.'
backend = "patch"
force_autoconf = False

applied = []

patch_data = ''

def usage():
    print '''
Usage: ./staging/patchinstall.py [options] [patchset ...]

Applies all Wine-Staging patch sets, or each specified patch set plus its
dependencies, to a Wine tree.

Options:
    -h, --help                  print this message
    -v, --version               print version information
    -a, --all                   apply all patches (except those excluded with -W)
    -W, --exclude <patchset>    exclude a patch
    --no-autoconf               do not run autoreconf and tools/make_requests
    --force-autoconf            run autoreconf and tools/make_requests after every patch
                                (ignored when not using git-am or git-am-C1 backends)
    --backend=<backend>         use the given backend to apply patches:
              patch             use the `gitapply.sh` tool (a wrapper around `patch`)
              git-am            use `git am`
              git-am-C1         use `git am -C1`
              git-apply         use `git apply`
    --ignore-missing            automatically add dependencies, but do not fail
                                if they are missing or disabled
    -r, --rebase-mode           alias for --backend=git-am-C1 --no-autoconf
    -d, --destdir=<path>        install to <path> (defaults to current working directory)
    --upstream-commit           print the Wine commit hash and exit
'''

def run(*args, **kwargs):
    print ' '.join(args[0])
    return subprocess.call(*args, **kwargs)

# return a patch to be shoved in patchlist below
def parse_def_file(name, path):
    deps = []
    if os.path.exists(path):
        with open(path) as f:
            for l in f.readlines():
                if l.lower().startswith('depends: '):
                    deps.append(l.split(' ')[1].strip())
                elif l.lower().strip() == 'disabled: true':
                    return None
    return deps

def apply_patch(patch):
    if backend == 'git-am':
        return run(['git','-C',winedir,'am',patch])
    elif backend == 'git-am-C1':
        return run(['git','-C',winedir,'am','-C1',patch])
    elif backend == 'patch':
        with open(patch) as f:
            print patchdir+'/gitapply.sh <',patch
            return subprocess.call([patchdir+'/gitapply.sh'],stdin=f)
    elif backend == 'git-apply':
        return run(['git','-C',winedir,'apply','--index',patch])

def run_autoconf(patch):
    if not force_autoconf: return

    if backend != 'git-am' and backend != 'git-am-C1':
        print 'Warning: ignoring --force-autoconf for backend ',backend

    need_autoreconf = False
    need_make_requests = False

    with open(patch) as f:
        for line in f.readlines():
            line = line.strip()
            if line == '--- a/configure.ac' or line == '--- a/aclocal.m4': need_autoreconf = True
            elif line == '--- a/server/protocol.def': need_make_requests = True

    if need_autoreconf:
        run(['autoreconf','-f'], cwd=winedir)
    if need_make_requests:
        run(['./tools/make_requests'], cwd=winedir)
    if need_autoreconf or need_make_requests:
        run(['git','-C',winedir,'commit','-a','--amend','--no-edit'])

def add_patch_data(patch):
    global patch_data
    author = ''
    subject = ''
    with open(patch) as f:
        for line in f.readlines():
            header = email.header.decode_header(line)
            if header[0][0] == 'From:':
                author = header[1][0]
            elif line[:5] == 'From:':
                author = line[6:line.index('<')-1]
            elif line[:8] == 'Subject:':
                subject = line[9:]
                if '[' in subject: subject = subject[subject.index(']') + 1:]
            if author and subject: break
        author = author.strip().strip('"').replace('\\','\\\\').replace('"','\\"')
        subject = subject.strip().replace('\\','\\\\').replace('"','\\"')
        patch_data += '+    {"%s", "%s", 1},\n' %(author, subject)

def apply_set(patchlist, name):
    if name in applied:
        return True
    for dep in patchlist[name]:
        if dep in patchlist and not apply_set(patchlist, dep):
            return False
    for patch in sorted(os.listdir(patchdir+'/'+name)):
        if patch.endswith('.patch') and patch.startswith('0'):
            patch_file = patchdir + '/' + name + '/' + patch
            if apply_patch(patch_file):
                print 'Failed to apply patch %s/%s' %(name, patch)
                return False
            run_autoconf(patch_file)
            add_patch_data(patch_file)
    applied.append(name)
    return True

def add_patchset(patchlist, name):
    path = patchdir + '/' + name
    if os.path.isdir(path):
        deps = parse_def_file(name, path + '/definition')
        if deps == None:
            print 'Error: attempt to apply %s, but it is disabled.' %name
            sys.exit(1)
        patchlist[name] = deps
        for dep in deps: add_patchset(patchlist, dep)

def main():
    global backend, force_autoconf, winedir
    patchlist = {}
    excluded = []
    no_autoconf = False
    ignore_missing = False

    try:
        opts, args = getopt.gnu_getopt(sys.argv[1:], 'ad:hrvW:', \
                ['all',
                 'backend=',
                 'destdir=',
                 'force-autoconf',
                 'ignore-missing',
                 'help',
                 'no-autoconf',
                 'upstream-commit',
                 'version'])
    except getopt.GetoptError as err:
        print str(err)
        sys.exit(2)

    for o, a in opts:
        if o == '-h' or o == '--help':
            usage()
            sys.exit(0)
        elif o == '-v' or o == '--version':
            with open(stagingdir + 'staging/VERSION') as f:
                print f.read().rstrip()
            print 'wine-staging is distributed under the GNU LGPL 2.1.'
            print 'See individual files for copyright information.'
            sys.exit(0)
        elif o == '-r' or o == '--rebase-mode':
            no_autoconf = True
            backend = "git-am-C1"
        elif o == '-W' or o == '--exclude':
            excluded.append(a)
        elif o == '-a' or o == '--all':
            for name in os.listdir(patchdir):
                path = patchdir + '/' + name
                if os.path.isdir(path):
                    deps = parse_def_file(name, path + '/definition')
                    if deps != None: # it's not disabled
                        patchlist[name] = deps
        elif o == '-d' or o == '--destdir':
            winedir = a
        elif o == '--backend':
            backend = a
        elif o == '--no-autoconf':
            no_autoconf = True
        elif o == '--force-autoconf':
            force_autoconf = True
        elif o == '--ignore-missing':
            ignore_missing = True
        elif o == '--upstream-commit':
            with open(stagingdir + 'staging/upstream-commit') as f:
                print f.read().rstrip();
            sys.exit(0)

    for a in args: add_patchset(patchlist, a)

    for p in excluded: del patchlist[p]

    if not os.access(winedir + '/tools/make_requests', os.F_OK):
        print "Target directory '%s' does not point to a Wine tree." %winedir
        sys.exit(1)

    if not patchlist:
        print 'No patches specified, either use -a or specify one or more patch sets as arguments.'
        sys.exit(1)

    # Check that all of our dependencies exist
    for p in patchlist:
        deps = patchlist[p]
        for d in deps:
            if d not in patchlist:
                if not ignore_missing:
                    print 'Error: unknown or disabled dependency %s of %s.' %(d,p)
                    sys.exit(1)
                else:
                    print 'Warning: unknown or disabled dependency %s of %s.' %(d,p)

    # Now try to apply each patch
    for p in sorted(patchlist.keys()):
        # Try to apply it
        if not apply_set(patchlist, p):
            sys.exit(1)

    if not no_autoconf and not force_autoconf:
        run(['autoreconf','-f'],cwd=winedir)
        run(['./tools/make_requests'],cwd=winedir)
    sys.exit(0)

if __name__ == '__main__':
    main()
