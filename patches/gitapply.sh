#!/usr/bin/env bash
#
# Wrapper to apply binary patches without git.
#
# Copyright (C) 2014-2016 Sebastian Lackner
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

nogit=0
tmpfile=""

# Show usage information about gitapply script
usage()
{
	echo ""
	echo "Usage: ./gitapply.sh [--nogit] [-d DIRECTORY]"
	echo ""
	echo "Reads patch data from stdin and applies the patch to the current"
	echo "directory or the directory given via commandline."
	echo ""
	echo "The patch file can contain both unified text patches as well as"
	echo "git binary patches."
	echo ""
}

# Critical error, abort
abort()
{
	if [ ! -z "$tmpfile" ]; then
		rm "$tmpfile"
		tmpfile=""
	fi
	echo "[PATCH] ERR: $1" >&2
	exit 1
}

# Show a warning
warning()
{
	echo "[PATCH] WRN: $1" >&2
}

# Calculate git sha1 hash
gitsha1()
{
	if [ -f "$1" ]; then
		echo -en "blob $(du -b "$1" | cut -f1)\x00" | cat - "$1" | sha1sum | cut -d' ' -f1
	else
		echo "0000000000000000000000000000000000000000"
	fi
}

# Determine size of a file (or zero, if it doesn't exist)
filesize()
{
	local size=$(du -b "$1" | cut -f1)
	if [ -z "$size" ]; then
		size="0"
	fi
	echo "$size"
}


# Parse environment variables
while [ "$#" -gt 0 ]; do
	cmd="$1"; shift
	case "$cmd" in

		--nogit)
			nogit=1
			;;

		-v)
			;;

		--directory=*)
			cd "${cmd#*=}"
			;;
		-d)
			cd "$1"; shift
			;;

		-R)
			abort "Reverse applying patches not supported yet with this tool."
			;;

		--help)
			usage
			exit 0
			;;

		*)
			warning "Unknown argument $cmd."
			;;
	esac
done

# Redirect to git apply if available
if [ "$nogit" -eq 0 ] && command -v git >/dev/null 2>&1; then
	exec git apply --whitespace=nowarn "$@"
	exit 1
fi

# Detect BSD - we check this first to error out as early as possible
if gzip -V 2>&1 | grep -q "BSD"; then
	echo "This script is not compatible with *BSD utilities. Please install git," >&2
	echo "which provides the same functionality and will be used instead." >&2
	exit 1
fi

# Check if GNU Awk is available
if ! command -v gawk >/dev/null 2>&1; then
	if ! awk -V 2>/dev/null | grep -q "GNU Awk"; then
		echo "This script requires GNU Awk (or alternatively git) to work properly." >&2
		exit 1
	fi

	gawk()
	{
		awk "$@"
	}
fi

# Check for missing depdencies
for dependency in gawk cut dd du grep gzip hexdump patch sha1sum; do
	if ! command -v "$dependency" >/dev/null 2>&1; then
		echo "Missing dependency: $dependency - please install this program and try again." >&2
		exit 1
	fi
done

# Workaround for new versions of awk, which assume that we want to use unicode
export LANG=C
export LC_ALL=C

# Decode base85 git data, prepend with a gzip header
awk_decode_b85='
BEGIN{
  git="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";
  b85="0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz!#$%&()*+-;<=>?@^_`{|}~";
  printf("\x1f\x8b\x08\x00\x00\x00\x00\x00");
  while (getline > 0){
    l = index(git, substr($0, 1, 1));
    if (l == 0){ exit 1; }
    p=2;
    while (l > 0 && p <= length($0)){
      a = index(b85, substr($0, p++, 1));
      b = index(b85, substr($0, p++, 1));
      c = index(b85, substr($0, p++, 1));
      d = index(b85, substr($0, p++, 1));
      e = index(b85, substr($0, p++, 1));
      if (a-- == 0 || b-- == 0 || c-- == 0 || d-- == 0 || e-- == 0){ exit 1; }
      n = (((a * 85 + b) * 85 + c) * 85 + d) * 85 + e;
      if (n > 4294967295){ exit 1; }
      a = n % 256; n /= 256;
      b = n % 256; n /= 256;
      c = n % 256; n /= 256;
      d = n % 256;
      if (l-- > 0) printf("%c", d);
      if (l-- > 0) printf("%c", c);
      if (l-- > 0) printf("%c", b);
      if (l-- > 0) printf("%c", a);
    }
    if (p != length($0) + 1 || l != 0){ exit 1; }
  }
}'

# Decodes the information from a git delta patch passed in as hex encoded
awk_decode_binarypatch='
function get_byte(a, b){ # usage: get_byte()
  if (length(__buffer) == 0){ if(getline __buffer <= 0){ exit 1; } }
  a = index(hex, substr(__buffer, 1, 1));
  b = index(hex, substr(__buffer, 2, 1));
  if (a-- == 0 || b-- == 0){ exit 1; }
  __buffer = substr(__buffer, 3); __pos++;
  return a * 16 + b;
}
function skip_bytes(n, m){ # usage: skip_bytes(n)
  if (length(__buffer) == 0){ if(getline __buffer <= 0){ exit 1; } }
  while (n >= (length(__buffer) / 2)){
    m = length(__buffer) / 2; n -= m; __pos += m;
    if(getline __buffer <= 0){ exit 1; }
  }
  if (n > 0){ __buffer = substr(__buffer, 1 + 2 * n); __pos += n; }
}
function get_delta_hdr_size(){ # usage: get_delta_hdr_size()
  cmd = get_byte(); size = and(cmd, 0x7f); i = 7;
  while (and(cmd, 0x80)){
    cmd = get_byte(); size += lshift(and(cmd, 0x7f), i); i += 7;
  }
  return size;
}
BEGIN{
  hex="0123456789ABCDEF"
  src_size = get_delta_hdr_size(); dst_size = get_delta_hdr_size();
  printf("S %d %d\n", src_size, dst_size);
  while (dst_size > 0){
    cmd = get_byte();
    if (and(cmd, 0x80)){
      cp_offs = 0; cp_size = 0;
      if (and(cmd, 0x01)){ cp_offs  = get_byte(); }
      if (and(cmd, 0x02)){ cp_offs += lshift(get_byte(), 8); }
      if (and(cmd, 0x04)){ cp_offs += lshift(get_byte(), 16); }
      if (and(cmd, 0x08)){ cp_offs += lshift(get_byte(), 24); }
      if (and(cmd, 0x10)){ cp_size  = get_byte(); }
      if (and(cmd, 0x20)){ cp_size += lshift(get_byte(), 8); }
      if (and(cmd, 0x40)){ cp_size += lshift(get_byte(), 16); }
      if (cp_size == 0){   cp_size  = 0x10000; }
      if (cp_offs + cp_size > src_size || cp_size > dst_size){ exit 1; }
      printf("1 %d %d\n", cp_offs, cp_size);
      dst_size -= cp_size;
    }else if (cmd){
      if (cmd > dst_size){ exit 1; }
      printf("2 %d %d\n", __pos, cmd);
      skip_bytes(cmd);
      dst_size -= cmd;
    }else{ exit 1; }
  }
  printf("E 0 0\n");
}'

# Find end of patch header
awk_eof_header='
BEGIN{
  ofs=1;
}
!/^(--- |\+\+\+ |old |deleted |new |copy |rename |similarity |index |GIT |literal |delta )/{
  ofs=0; exit 0;
}
END{
  print FNR+ofs;
}'

# Find end of text patch
awk_eof_textpatch='
BEGIN{
  ofs=1;
}
!/^(@| |\+|-|\\)/{
  ofs=0; exit 0;
}
END{
  print FNR+ofs;
}'

# Find end of git binary patch
awk_eof_binarypatch='
BEGIN{
  ofs=1;
}
!/^[A-Za-z]/{
  ofs=0; exit 0;
}
END{
  print FNR+ofs;
}'


# Create a temporary file containing the patch - NOTE: even if the user
# provided a filename it still makes sense to work with a temporary file,
# to avoid changes of the content while this script is active.
tmpfile=$(mktemp)
if [ ! -f "$tmpfile" ]; then
	tmpfile=""
	abort "Unable to create temporary file for patch."
elif ! cat > "$tmpfile"; then
	abort "Patch truncated."
fi

# Go through the different patch sections
lastoffset=1
for offset in $(gawk '/^diff --git /{ print FNR; }' "$tmpfile"); do

	# Check part between end of last patch and start of current patch
	if [ "$lastoffset" -gt "$offset" ]; then
		abort "Unable to split patch. Is this a proper git patch?"
	elif [ "$lastoffset" -lt "$offset" ]; then
		tmpoffset=$((offset - 1))
		if sed -n "$lastoffset,$tmpoffset p" "$tmpfile" | grep -q '^\(@@ -\|--- \|+++ \)'; then
			abort "Patch corrupted or not created with git."
		fi
	fi

	# Find out the size of the patch header
	tmpoffset=$((offset + 1))
	tmpoffset=$(sed -n "$tmpoffset,\$ p" "$tmpfile" | gawk "$awk_eof_header")
	hdroffset=$((offset + tmpoffset))

	# Parse all important fields of the header
	patch_oldname=""
	patch_newname=""
	patch_oldsha1=""
	patch_newsha1=""
	patch_is_binary=0
	patch_binary_type=""
	patch_binary_size=""

	tmpoffset=$((hdroffset - 1))
	while IFS= read -r line; do
		if [ "$line" == "GIT binary patch" ]; then
			patch_is_binary=1

		elif [[ "$line" =~ ^diff\ --git\ ([^ ]*)\ ([^ ]*)$  ]]; then
			patch_oldname="${BASH_REMATCH[1]}"
			patch_newname="${BASH_REMATCH[2]}"

		elif [[ "$line" =~ ^---\ (.*)$ ]]; then
			patch_oldname="${BASH_REMATCH[1]}"

		elif [[ "$line" =~ ^\+\+\+\ (.*)$ ]]; then
			patch_newname="${BASH_REMATCH[1]}"

		elif [[ "$line" =~ ^index\ ([a-fA-F0-9]*)\.\.([a-fA-F0-9]*) ]]; then
			patch_oldsha1="${BASH_REMATCH[1]}"
			patch_newsha1="${BASH_REMATCH[2]}"

		elif [[ "$line" =~ ^(literal|delta)\ ([0-9]+)$ ]]; then
			patch_binary_type="${BASH_REMATCH[1]}"
			patch_binary_size="${BASH_REMATCH[2]}"

		fi
	done < <(sed -n "$offset,$tmpoffset p" "$tmpfile")

	# Remove first path components, which are always a/ and b/ for git patches
	if [[ "$patch_oldname" =~ ^a/(.*)$ ]]; then
		patch_oldname="${BASH_REMATCH[1]}"
	elif [ "$patch_oldname" != "/dev/null" ]; then
		abort "Old name doesn't start with a/."
	fi
	if [[ "$patch_newname" =~ ^b/(.*)$ ]]; then
		patch_newname="${BASH_REMATCH[1]}"
	elif [ "$patch_newname" != "/dev/null" ]; then
		abort "New name doesn't start with b/."
	fi

	# Short progress message
	echo "patching $patch_newname"

	# If its a textual patch, then use 'patch' to apply it.
	if [ "$patch_is_binary" -eq 0 ]; then

		# Find end of textual patch
		tmpoffset=$(sed -n "$hdroffset,\$ p" "$tmpfile" | gawk "$awk_eof_textpatch")
		lastoffset=$((hdroffset + tmpoffset - 1))

		# Apply textual patch
		tmpoffset=$((lastoffset - 1))
		if ! sed -n "$offset,$tmpoffset p" "$tmpfile" | patch -p1 -s -f; then
			abort "Textual patch did not apply, aborting."
		fi

		continue
	fi

	# It is a binary patch - check that requirements are fulfilled
	if [ "$patch_binary_type" != "literal" ] && [ "$patch_binary_type" != "delta" ]; then
		abort "Unknown binary patch type."

	elif [ -z "$patch_oldsha1" ] || [ -z "$patch_newsha1" ]; then
		abort "Missing index header, sha1 sums required for binary patch."

	elif [ "$patch_oldname" != "$patch_newname" ]; then
		abort "Stripped old and new name doesn't match for binary patch."
	fi

	# Ensure that checksum of old file matches
	sha=$(gitsha1 "$patch_oldname")
	if [ "$patch_oldsha1" != "$sha" ]; then
		abort "Checksum mismatch for $patch_oldname (expected $patch_oldsha1, got $sha)."
	fi

	# Find end of binary patch
	tmpoffset=$(sed -n "$hdroffset,\$ p" "$tmpfile" | gawk "$awk_eof_binarypatch")
	lastoffset=$((hdroffset + tmpoffset - 1))

	# Special case - deleting the whole file
	if [ "$patch_newsha1" == "0000000000000000000000000000000000000000" ] &&
			[ "$patch_binary_size" -eq 0 ] && [ "$patch_binary_type" == "literal" ]; then

		# Applying the patch just means deleting the file
		if [ -f "$patch_oldname" ] && ! rm "$patch_oldname"; then
			abort "Unable to delete file $patch_oldname."
		fi

		continue
	fi

	# Create temporary file for literal patch
	literal_tmpfile=$(mktemp)
	if [ ! -f "$literal_tmpfile" ]; then
		abort "Unable to create temporary file for binary patch."
	fi

	# Decode base85 and gzip compression
	tmpoffset=$((lastoffset - 1))
	sed -n "$hdroffset,$tmpoffset p" "$tmpfile" | gawk "$awk_decode_b85" | gzip -dc > "$literal_tmpfile" 2>/dev/null
	if [ "$patch_binary_size" -ne "$(filesize "$literal_tmpfile")" ]; then
		rm "$literal_tmpfile"
		abort "Uncompressed binary patch has wrong size."
	fi

	# Convert delta to literal patch
	if [ "$patch_binary_type" == "delta" ]; then

		# Create new temporary file for literal patch
		delta_tmpfile="$literal_tmpfile"
		literal_tmpfile=$(mktemp)
		if [ ! -f "$literal_tmpfile" ]; then
			rm "$delta_tmpfile"
			abort "Unable to create temporary file for binary patch."
		fi

		patch_binary_complete=0
		patch_binary_destsize=0

		while read cmd arg1 arg2; do
			if [ "$cmd" == "S" ]; then
				[ "$arg1" -eq "$(filesize "$patch_oldname")" ] || break
				patch_binary_destsize="$arg2"

			elif [ "$cmd" == "1" ]; then
				dd if="$patch_oldname" bs=1 skip="$arg1" count="$arg2" >> "$literal_tmpfile" 2>/dev/null || break

			elif [ "$cmd" == "2" ]; then
				dd if="$delta_tmpfile" bs=1 skip="$arg1" count="$arg2" >> "$literal_tmpfile" 2>/dev/null || break

			elif [ "$cmd" == "E" ]; then
				patch_binary_complete=1

			else break; fi
		done < <(hexdump -v -e '32/1 "%02X" "\n"' "$delta_tmpfile" | gawk "$awk_decode_binarypatch")

		rm "$delta_tmpfile"

		if [ "$patch_binary_complete" -eq 0 ]; then
			rm "$literal_tmpfile"
			abort "Unable to parse full patch."

		elif [ "$patch_binary_destsize" -ne "$(filesize "$literal_tmpfile")" ]; then
			rm "$literal_tmpfile"
			abort "Unpacked delta patch has wrong size."
		fi
	fi

	# Ensure that checksum of literal patch matches
	sha=$(gitsha1 "$literal_tmpfile")
	if [ "$patch_newsha1" != "$sha" ]; then
		rm "$literal_tmpfile"
		abort "Checksum mismatch for patched $patch_newname (expected $patch_newsha1, got $sha)."
	fi

	# Apply the patch - copy literal patch to destination path
	if ! cp "$literal_tmpfile" "$patch_newname"; then
		rm "$literal_tmpfile"
		abort "Unable to replace $patch_newname with patched file."
	fi

	rm "$literal_tmpfile"
done

# Check last remaining part for unparsed patches
if sed -n "$lastoffset,\$ p" "$tmpfile" | grep -q '^\(@@ -\|--- \|+++ \)'; then
	abort "Patch corrupted or not created with git."
fi

# Delete temp file (if any)
if [ ! -z "$tmpfile" ]; then
	rm "$tmpfile"
	tmpfile=""
fi

# Success
exit 0
