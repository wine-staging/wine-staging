#!/bin/sh

sha=`git -C ./staging/wine/ rev-parse origin/master`
echo $sha > ./staging/upstream-commit
echo "Wine Staging $1" > ./staging/VERSION

#git -C ./staging/wine/ describe --tags 15aec461174ae2f2568d63fa83f32576ede05a9c
# Get version from it

git commit -am "Release v$1"
git tag -a v$1 -m "Release v$1"
