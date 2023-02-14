#!/usr/bin/env python2

import getopt, os, subprocess, sys

winedir = './staging/wine/'
patchdir = os.path.abspath('./patches/')
applied = []

def usage():
    print 'newupdate.py - new script to rebase patches'
    print 'usage: ./staging/newupdate.py'
    print 'Applies every patch to a Wine tree. On failure, prints the name of'
    print 'the patch that failed to apply and cleans up the index. On success,'
    print 'leaves every patch applied, runs autoreconf -f and'
    print 'tools/make_requests to prepare for a build, and runs'
    print './staging/patchupdate.py --skip-bugs --skip-checks'
    print 'to update patchinstall.sh.'

# return a patch to be shoved in patchlist below
def parse_def_file(name, path):
    deps = []
    if os.path.exists(path):
        with open(path) as z:
            for l in z.readlines():
                if l.lower().startswith('depends: '):
                    deps.append(l.split(' ')[1].strip())
                elif l.lower().strip() == 'disabled: true':
                    return None
    return deps

def apply_set(patchlist, name):
    if name in applied:
        return True
    for dep in patchlist[name]:
        if not apply_set(patchlist, dep):
            return False
    print 'Applying',name
    for patch in sorted(os.listdir(patchdir+'/'+name)):
        if patch.endswith('.patch') and patch.startswith('0') and subprocess.call(['git','-C',winedir,'apply','--index',patchdir+'/'+name+'/'+patch]):
            print 'Failed to apply patch %s/%s' %(name, patch)
            return False
    applied.append(name)
    return True

def main():
    try:
        opts, args = getopt.getopt(sys.argv[1:], 'h', ['help'])
    except getopt.GetoptError as err:
        print str(err)
        sys.exit(2)

    for o, a in opts:
        if o == '-h':
            usage()
            sys.exit(0)

    # Build a list of patches
    # key is patch name, value is list of deps
    # disabled patches are not included
    patchlist = {}

    for name in os.listdir(patchdir):
        path = patchdir + '/' + name
        if not os.path.isdir(path): continue
        deps = parse_def_file(name, path + '/definition')
        if deps != None: # it's not disabled
            patchlist[name] = deps

    # Check that all of our dependencies exist
    for p in patchlist:
        deps = patchlist[p]
        for d in deps:
            if d not in patchlist:
                print 'Error: unknown or disabled dependency %s of %s.' %(d,p)
                sys.exit(1)

    # Now try to apply each patch
    for p in sorted(patchlist.keys()):
        # Try to apply it
        if not apply_set(patchlist, p):
            # clean up
            subprocess.call(['git','-C',winedir,'reset','--hard','HEAD','--quiet'])
            sys.exit(1)

    # we successfully applied everything, autogen some files so we can build
    print 'Calling autoreconf -f...'
    subprocess.call(['autoreconf','-f'],cwd=winedir)
    print 'Calling tools/make_requests...'
    subprocess.call(['./tools/make_requests'],cwd=winedir)
    sys.exit(0)

if __name__ == '__main__':
    main()
