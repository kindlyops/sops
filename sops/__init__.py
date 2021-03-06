#!/usr/bin/env python
# -*- coding: utf-8 -*-
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Contributor: Julien Vehent <jvehent@mozilla.com> [:ulfr]
# Contributor: Daniel Thornton <daniel@relud.com>
# Contributor: Alexis Metaireau <alexis@mozilla.com> [:alexis]
# Contributor: Rémy Hubscher <natim@mozilla.com> [:natim]

from __future__ import print_function, unicode_literals
import argparse
import hashlib
import os
import re
import subprocess
import sys
import tempfile
from base64 import b64encode, b64decode
from datetime import datetime
from socket import gethostname
from textwrap import dedent

import boto3
import ruamel.yaml
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, modes, algorithms

if sys.version_info[0] == 2 and sys.version_info[1] == 6:
    # python2.6 needs simplejson and ordereddict
    import simplejson as json
    from ordereddict import OrderedDict
else:
    import json
    from collections import OrderedDict

if sys.version_info[0] == 3:
    raw_input = input

VERSION = 0.9

DESC = """
`sops` supports AWS KMS and PGP encryption:
    * To encrypt or decrypt a document with AWS KMS, specify the KMS ARN
      in the `-k` flag or in the ``SOPS_KMS_ARN`` environment variable.
      (you need valid credentials in ~/.aws/credentials or in your env)
    * To encrypt or decrypt using PGP, specify the PGP fingerprint in the
      `-p` flag or in the ``SOPS_PGP_FP`` environment variable.

To use multiple KMS or PGP keys, separate them by commas. For example:
    $ sops -p "10F2[...]0A, 85D[...]B3F21" file.yaml

The -p and -k flags are ignored if the document already contains master
keys. To add/remove master keys in existing documents, open then with -s
and edit the `sops` branch directly.

By default, editing is done in vim, and will use the $EDITOR env if set.

Version {version} - See the Readme at github.com/mozilla/sops
""".format(version=VERSION)

DEFAULT_YAML = """# Welcome to SOPS. This is the default template.
# Remove these lines and add your data.
# Don't modify the `sops` section, it contains key material.
example_key: example_value
example_array:
    - example_value1
    - example_value2
example_multiline: |
    this is a
    multiline
    entry
example_number: 1234.5678
example:
    nested:
        values: delete_me
"""

DEFAULT_JSON = """{
"example_key": "example_value",
"example_array": [
    "example_value1",
    "example_value2"
],
"example_number": 1234.5678
}"""

DEFAULT_TEXT = """Welcome to SOPS!
Remove this text and add your content to the file.

"""

NOW = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')

INPUT_VERSION = VERSION


def main():
    argparser = argparse.ArgumentParser(
        usage='sops <file>',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description='SOPS - encrypted files editor that uses AWS KMS and PGP',
        epilog=dedent(DESC))
    argparser.add_argument('file',
                           help="file to edit; create it if it doesn't exist")
    argparser.add_argument('-k', '--kms', dest='kmsarn',
                           help="comma separated list of KMS ARNs")
    argparser.add_argument('-p', '--pgp', dest='pgpfp',
                           help="comma separated list of PGP fingerprints")
    argparser.add_argument('-d', '--decrypt', action='store_true',
                           dest='decrypt',
                           help="decrypt <file> and print it to stdout")
    argparser.add_argument('-e', '--encrypt', action='store_true',
                           dest='encrypt',
                           help="encrypt <file> and print it to stdout")
    argparser.add_argument('-i', '--in-place', action='store_true',
                           dest='in_place',
                           help="write output back to <file> instead "
                                "of stdout for encrypt/decrypt")
    argparser.add_argument('-r', '--rotate', action='store_true',
                           dest='rotate',
                           help="generate a new data encryption key and "
                                "encrypt all values with the new key")
    argparser.add_argument('--extract', dest='tree_path',
                           help="extract a specific key or branch from the "
                                "input JSON or YAML document. (decrypt mode "
                                "only). ex: --extract '[\"somekey\"][0]'")
    argparser.add_argument('--input-type', dest='input_type',
                           help="input type (yaml, json, ...), "
                                "if undef, use file extension")
    argparser.add_argument('--output-type', dest='output_type',
                           help="output type (yaml, json, ...), "
                                "if undef, use input type")
    argparser.add_argument('-s', '--show_master_keys', action='store_true',
                           dest='show_master_keys',
                           help="display master encryption keys in the file "
                                "during editing (off by default).")
    argparser.add_argument('--ignore-mac', action='store_true',
                           dest='ignore_mac',
                           help="ignore Message Authentication Code "
                                "during decryption")
    args = argparser.parse_args()

    kms_arns = ""
    if 'SOPS_KMS_ARN' in os.environ:
        kms_arns = os.environ['SOPS_KMS_ARN']
    if args.kmsarn:
        kms_arns = args.kmsarn

    pgp_fps = ""
    if 'SOPS_PGP_FP' in os.environ:
        pgp_fps = os.environ['SOPS_PGP_FP']
    if args.pgpfp:
        pgp_fps = args.pgpfp

    # use input type as output type if not specified
    if args.input_type:
        itype = args.input_type
    else:
        itype = detect_filetype(args.file)

    if args.output_type:
        otype = args.output_type
    else:
        otype = itype

    tree, need_key, existing_file = initialize_tree(args.file, itype,
                                                    kms_arns=kms_arns,
                                                    pgp_fps=pgp_fps)
    if not existing_file:
        if (args.encrypt or args.decrypt):
            panic("cannot operate on non-existent file", error_code=100)
        else:
            print("%s doesn't exist, creating it." % args.file)

    if args.rotate:
        # if rotate is set, force a data key generation even if one exists
        need_key = True

    if args.encrypt:
        # Encrypt mode: encrypt, display and exit
        key, tree = get_key(tree, need_key)
        tree = walk_and_encrypt(tree, key)
        dest = '/dev/stdout'
        if args.in_place:
            dest = args.file
        if otype == "bytes":
            otype = "json"
        write_file(tree, path=dest, filetype=otype)
        sys.exit(0)

    if args.decrypt:
        # Decrypt mode: decrypt, display and exit
        key, tree = get_key(tree)
        tree = walk_and_decrypt(tree, key, ignoreMac=args.ignore_mac)
        if not args.show_master_keys:
            tree.pop('sops', None)
        dest = '/dev/stdout'
        if args.in_place:
            dest = args.file
        if args.tree_path:
            tree = truncate_tree(tree, args.tree_path)
        write_file(tree, path=dest, filetype=otype)
        sys.exit(0)

    # EDIT Mode: decrypt, edit, encrypt and save
    key, tree = get_key(tree, need_key)

    # we need a stash to save the IV and AAD and reuse them
    # if a given value has not changed during editing
    stash = dict()
    stash['sops'] = dict(tree['sops'])
    if existing_file:
        tree = walk_and_decrypt(tree, key, stash=stash,
                                ignoreMac=args.ignore_mac)

    # hide the sops branch during editing
    if not args.show_master_keys:
        tree.pop('sops', None)

    # the decrypted tree is written to a tempfile and an editor
    # is opened on the file
    tmppath = write_file(tree, filetype=otype)
    tmpstamp = os.stat(tmppath)
    print("temp file created at %s" % tmppath, file=sys.stderr)

    # open an editor on the file and, if the file is yaml or json,
    # verify that it doesn't contain errors before continuing
    valid_syntax = False
    has_master_keys = False
    while not valid_syntax or not has_master_keys:
        run_editor(tmppath)
        try:
            valid_syntax = validate_syntax(tmppath, otype)
        except Exception as e:
            try:
                print("Syntax error: %s\nPress a key to return into "
                      "the editor, or ctrl+c to exit without saving." % e,
                      file=sys.stderr)
                raw_input()
            except KeyboardInterrupt:
                os.remove(tmppath)
                panic("ctrl+c captured, exiting without saving", 85)

        if args.show_master_keys:
            # use the sops data from the file
            tree = load_file_into_tree(tmppath, otype)
        else:
            # sops branch was removed for editing, restoring it
            tree = load_file_into_tree(tmppath, otype,
                                       restore_sops=stash['sops'])
        if check_master_keys(tree):
            has_master_keys = True
        else:
            try:
                print("Could not find a valid master key to encrypt the "
                      "data key with.\nAdd at least one KMS or PGP "
                      "master key to the `sops` branch,\nor ctrl+c to "
                      "exit without saving.")
                raw_input()
            except KeyboardInterrupt:
                os.remove(tmppath)
                panic("ctrl+c captured, exiting without saving", 85)

    # verify if file has been modified, and if not, just exit
    tmpstamp2 = os.stat(tmppath)
    if tmpstamp == tmpstamp2:
        os.remove(tmppath)
        panic("%s has not been modified, exit without writing" % args.file,
              error_code=200)

    tree = walk_and_encrypt(tree, key, stash=stash)
    tree = update_master_keys(tree, key)
    os.remove(tmppath)

    # always store encrypted binary files in a json enveloppe
    if otype == "bytes":
        otype = "json"
    path = write_file(tree, path=args.file, filetype=otype)
    print("file written to %s" % (path), file=sys.stderr)
    sys.exit(0)


def detect_filetype(file):
    """Detect the type of file based on its extension.
    Return a string that describes the format: `bytes`, `yaml`, `json`
    """
    base, ext = os.path.splitext(file)
    if (ext == '.yaml') or (ext == '.yml'):
        return 'yaml'
    elif ext == '.json':
        return 'json'
    return 'bytes'


def initialize_tree(path, itype, kms_arns=None, pgp_fps=None):
    """ Try to load the file from path in a tree, and failing that,
        initialize a new tree using default data
    """
    tree = OrderedDict()
    need_key = False
    try:
        existing_file = os.stat(path)
    except:
        existing_file = False
    if existing_file:
        # read the encrypted file from disk
        tree = load_file_into_tree(path, itype)
        tree, need_key = verify_or_create_sops_branch(tree,
                                                      kms_arns=kms_arns,
                                                      pgp_fps=pgp_fps)
        # try to set the input version to the one set in the file
        try:
            global INPUT_VERSION
            INPUT_VERSION = tree['sops']['version']
        except:
            None
    else:
        # load a new tree using template data
        if itype == "yaml":
            tree = ruamel.yaml.load(DEFAULT_YAML, ruamel.yaml.RoundTripLoader)
        elif itype == "json":
            tree = json.loads(DEFAULT_JSON, object_pairs_hook=OrderedDict)
        else:
            tree['data'] = DEFAULT_TEXT
        tree, need_key = verify_or_create_sops_branch(tree, kms_arns, pgp_fps)
    return tree, need_key, existing_file


def load_file_into_tree(path, filetype, restore_sops=None):
    """Load the tree.

    Read data from `path` using format defined by `filetype`.
    Return a dictionary with the data.

    """
    tree = OrderedDict()
    with open(path, "rb") as fd:
        if filetype == 'yaml':
            tree = ruamel.yaml.load(fd, ruamel.yaml.RoundTripLoader)
        elif filetype == 'json':
            data = fd.read()
            if isinstance(data, bytes):
                data = data.decode('utf-8')
            tree = json.loads(data, object_pairs_hook=OrderedDict)
        else:
            data = fd.read()
            # try to guess what type of file it is. It may be a previously sops
            # encrypted file, in which case it's in JSON format. If not, load
            # the bytes as such in the 'data' key.
            try:
                tree = json.loads(data.decode('utf-8'),
                                  object_pairs_hook=OrderedDict)
                if "version" not in tree['sops']:
                    tree['data'] = data
            except:
                tree['data'] = data
    if restore_sops:
        tree['sops'] = restore_sops.copy()
    return tree


def verify_or_create_sops_branch(tree, kms_arns=None, pgp_fps=None):
    """Verify or create the sops branch in the tree.

    If the current tree doesn't have a sops branch with either kms or pgp
    information, create it using the content of the global variables and
    indicate that an encryption is needed when returning.

    """
    need_new_data_key = False
    if 'sops' not in tree:
        tree['sops'] = dict()
        tree['sops']['attention'] = 'This section contains key material' + \
            ' that should only be modified with extra care. See `sops -h`.'
        tree['sops']['version'] = VERSION

    if 'kms' in tree['sops'] and isinstance(tree['sops']['kms'], list):
        # check that we have at least one ARN to work with
        for entry in tree['sops']['kms']:
            if 'arn' in entry and entry['arn'] != "" and entry['enc'] != "":
                return tree, need_new_data_key

    # if we're here, no arn was found
    if 'pgp' in tree['sops'] and isinstance(tree['sops']['pgp'], list):
        # check that we have at least one fingerprint to work with
        for entry in tree['sops']['pgp']:
            if 'fp' in entry and entry['fp'] != "" and entry['enc'] != "":
                return tree, need_new_data_key

    # if we're here, no pgp fingerprint was found either
    has_at_least_one_method = False
    need_new_data_key = True
    if not (kms_arns is None):
        tree, has_at_least_one_method = parse_kms_arn(tree, kms_arns)
    if not (pgp_fps is None):
        tree, has_at_least_one_method = parse_pgp_fp(tree, pgp_fps)
    if not has_at_least_one_method:
        panic("Error: No KMS ARN or PGP Fingerprint found to encrypt the data "
              "key, read the help (-h) for more information.", 111)
    return tree, need_new_data_key


def parse_kms_arn(tree, kms_arns):
    """Take a string that contains one or more KMS ARNs, possibly with roles,
       and transform them it into KMS entries of the sops tree
    """
    has_at_least_one_method = False
    tree['sops']['kms'] = list()
    for arn in kms_arns.split(','):
        arn = arn.replace(" ", "")
        entry = {}
        rolepos = arn.find("+arn:aws:iam::")
        if rolepos > 0:
            entry = {"arn": arn[:rolepos], "role": arn[rolepos+1:]}
        else:
            entry = {"arn": arn}
        tree['sops']['kms'].append(entry)
        has_at_least_one_method = True
    return tree, has_at_least_one_method


def parse_pgp_fp(tree, pgp_fps):
    """Take a string of PGP fingerprint
       and create pgp entries in the sops tree
    """
    has_at_least_one_method = False
    tree['sops']['pgp'] = list()
    for fp in pgp_fps.split(','):
        entry = {"fp": fp.replace(" ", "")}
        tree['sops']['pgp'].append(entry)
        has_at_least_one_method = True
    return tree, has_at_least_one_method


def update_master_keys(tree, key):
    """ If master keys have been added to the SOPS branch, encrypt the data key
        with them, and store the new encrypted values.
    """
    if 'kms' in tree['sops']:
        if not isinstance(tree['sops']['kms'], list):
            panic("invalid KMS format in SOPS branch, must be a list")
        i = -1
        for entry in tree['sops']['kms']:
            i += 1
            if not ('enc' in entry) or entry['enc'] == "":
                print("updating kms entry")
                updated = encrypt_key_with_kms(key, entry)
                tree['sops']['kms'][i] = updated
    if 'pgp' in tree['sops']:
        if not isinstance(tree['sops']['pgp'], list):
            panic("invalid PGP format in SOPS branch, must be a list")
        i = -1
        for entry in tree['sops']['pgp']:
            i += 1
            if not ('enc' in entry) or entry['enc'] == "":
                print("updating pgp entry")
                updated = encrypt_key_with_pgp(key, entry)
                tree['sops']['pgp'][i] = updated

    # update version number if newer than current
    if 'version' in tree['sops']:
        if tree['sops']['version'] < VERSION:
            tree['sops']['version'] = VERSION
    else:
        tree['sops']['version'] = VERSION

    return tree


def check_master_keys(tree):
    """ Make sure that we have at least one valid master key to encrypt
        the data key with
    """
    if 'kms' in tree['sops']:
        for entry in tree['sops']['kms']:
            if 'arn' in entry and entry['arn'] != "":
                return True
    if 'pgp' in tree['sops']:
        for entry in tree['sops']['pgp']:
            if 'fp' in entry and entry['fp'] != "":
                return True
    return False


def walk_and_decrypt(branch, key, aad=b'', stash=None, digest=None,
                     isRoot=True, ignoreMac=False):
    """Walk the branch recursively and decrypt leaves."""
    if isRoot and not ignoreMac:
        digest = hashlib.sha512()
    carryaad = aad
    for k, v in branch.items():
        if k == 'sops' and isRoot:
            continue    # everything under the `sops` key stays in clear
        nstash = dict()
        caad = aad
        if INPUT_VERSION >= 0.9:
            caad = aad + k.encode('utf-8') + b':'
        else:
            caad = carryaad
            caad += k.encode('utf-8')
            carryaad = caad
        if stash:
            stash[k] = {'has_stash': True}
            nstash = stash[k]
        if isinstance(v, dict):
            branch[k] = walk_and_decrypt(v, key, aad=caad, stash=nstash,
                                         digest=digest, isRoot=False)
        elif isinstance(v, list):
            branch[k] = walk_list_and_decrypt(v, key, aad=caad, stash=nstash,
                                              digest=digest)
        elif isinstance(v, ruamel.yaml.scalarstring.PreservedScalarString):
            ev = decrypt(v, key, aad=caad, stash=nstash, digest=digest)
            branch[k] = ruamel.yaml.scalarstring.PreservedScalarString(ev)
        else:
            branch[k] = decrypt(v, key, aad=caad, stash=nstash, digest=digest)

    if isRoot and not ignoreMac:
        # compute the hash computed on values with the one stored
        # in the file. If they match, all is well.
        if not ('mac' in branch['sops']):
            panic("'mac' not found, unable to verify file integrity", 52)
        h = digest.hexdigest().upper()
        # We know the original hash is trustworthy because it is encrypted
        # with the data key and authenticated using the lastmodified timestamp
        orig_h = decrypt(branch['sops']['mac'], key,
                         aad=branch['sops']['lastmodified'].encode('utf-8'))
        if h != orig_h:
            panic("Checksum verification failed!\nexpected %s\nbut got  %s" %
                  (orig_h, h), 51)

    return branch


def walk_list_and_decrypt(branch, key, aad=b'', stash=None, digest=None):
    """Walk a list contained in a branch and decrypts its values."""
    nstash = dict()
    kl = []
    for i, v in enumerate(list(branch)):
        if stash:
            stash[i] = {'has_stash': True}
            nstash = stash[i]
        if isinstance(v, dict):
            kl.append(walk_and_decrypt(v, key, aad=aad, stash=nstash,
                                       digest=digest, isRoot=False))
        elif isinstance(v, list):
            kl.append(walk_list_and_decrypt(v, key, aad=aad, stash=nstash,
                                            digest=digest))
        else:
            kl.append(decrypt(v, key, aad=aad, stash=nstash, digest=digest))
    return kl


def decrypt(value, key, aad=b'', stash=None, digest=None):
    """Return a decrypted value."""
    valre = b'^ENC\[AES256_GCM,data:(.+),iv:(.+),tag:(.+)'
    # extract fields using a regex
    if INPUT_VERSION >= 0.8:
        valre += b',type:(.+)'
    valre += b'\]'
    res = re.match(valre, value.encode('utf-8'))
    # if the value isn't in encrypted form, return it as is
    if res is None:
        return value
    enc_value = b64decode(res.group(1))
    iv = b64decode(res.group(2))
    tag = b64decode(res.group(3))
    valtype = 'str'
    if INPUT_VERSION >= 0.8:
        valtype = res.group(4)
    decryptor = Cipher(algorithms.AES(key),
                       modes.GCM(iv, tag),
                       default_backend()
                       ).decryptor()
    decryptor.authenticate_additional_data(aad)
    cleartext = decryptor.update(enc_value) + decryptor.finalize()

    if stash:
        # save the values for later if we need to reencrypt
        stash['iv'] = iv
        stash['aad'] = aad
        stash['cleartext'] = cleartext

    if digest:
        digest.update(cleartext)

    if valtype == b'bytes':
        return cleartext
    if valtype == b'str':
        # Welcome to python compatibility hell... :(
        # Python 2 treats everything as str, but python 3 treats bytes and str
        # as different types. So if a file was encrypted by sops with py2, and
        # contains bytes data, it will have type 'str' and py3 will decode
        # it as utf-8. This will result in a UnicodeDecodeError exception
        # because random bytes are not unicode. So the little try block below
        # catches it and returns the raw bytes if the value isn't unicode.
        cv = cleartext
        try:
            cv = cleartext.decode('utf-8')
        except UnicodeDecodeError:
            return cleartext
        return cv
    if valtype == b'int':
        return int(cleartext.decode('utf-8'))
    if valtype == b'float':
        return float(cleartext.decode('utf-8'))
    if valtype == b'bool':
        if cleartext.lower() == b'true':
            return True
        return False
    panic("unknown type "+valtype, 23)


def walk_and_encrypt(branch, key, aad=b'', stash=None,
                     isRoot=True, digest=None):
    """Walk the branch recursively and encrypts its leaves."""
    if isRoot:
        digest = hashlib.sha512()
    for k, v in branch.items():
        if k == 'sops' and isRoot:
            continue    # everything under the `sops` key stays in clear
        caad = aad + k.encode('utf-8') + b':'
        nstash = dict()
        if stash and k in stash:
            nstash = stash[k]
        if isinstance(v, dict):
            # recursively walk the tree
            branch[k] = walk_and_encrypt(v, key, aad=caad, stash=nstash,
                                         digest=digest, isRoot=False)
        elif isinstance(v, list):
            branch[k] = walk_list_and_encrypt(v, key, aad=caad, stash=nstash,
                                              digest=digest)
        elif isinstance(v, ruamel.yaml.scalarstring.PreservedScalarString):
            ev = encrypt(v, key, aad=caad, stash=nstash, digest=digest)
            branch[k] = ruamel.yaml.scalarstring.PreservedScalarString(ev)
        else:
            branch[k] = encrypt(v, key, aad=caad, stash=nstash, digest=digest)
    if isRoot:
        branch['sops']['lastmodified'] = NOW
        # finalize and store the message authentication code in encrypted form
        h = str()
        h = digest.hexdigest().upper()
        mac = encrypt(h, key,
                      aad=branch['sops']['lastmodified'].encode('utf-8'))
        branch['sops']['mac'] = mac
    return branch


def walk_list_and_encrypt(branch, key, aad=b'', stash=None, digest=None):
    """Walk a list contained in a branch and encrypts its values."""
    nstash = dict()
    kl = []
    for i, v in enumerate(list(branch)):
        if stash and i in stash:
            nstash = stash[i]
        if isinstance(v, dict):
            kl.append(walk_and_encrypt(v, key, aad=aad, stash=nstash,
                                       digest=digest, isRoot=False))
        elif isinstance(v, list):
            kl.append(walk_list_and_encrypt(v, key, aad=aad, stash=nstash,
                                            digest=digest))
        else:
            kl.append(encrypt(v, key, aad=aad, stash=nstash,
                              digest=digest))
    return kl


def encrypt(value, key, aad=b'', stash=None, digest=None):
    """Return an encrypted string of the value provided."""
    # save the original type
    # the order in which we do this matters. For example, a bool
    # is also an int, but an int isn't a bool, so we test for bool first
    if isinstance(value, str) or \
       (sys.version_info[0] == 2 and isinstance(value, unicode)):  # noqa
        valtype = 'str'
    elif isinstance(value, bool):
        valtype = 'bool'
    elif isinstance(value, int):
        valtype = 'int'
    elif isinstance(value, float):
        valtype = 'float'
    else:
        valtype = 'bytes'

    if not isinstance(value, bytes):
        # if not bytes, convert to bytes
        value = str(value).encode('utf-8')

    if digest:
        digest.update(value)

    # if we have a stash, and the value of cleartext has not changed,
    # attempt to take the IV.
    # if the stash has no existing value, or the cleartext has changed,
    # generate new IV.
    if stash and 'cleartext' in stash and stash['cleartext'] == value:
        iv = stash['iv']
    else:
        iv = os.urandom(32)
    encryptor = Cipher(algorithms.AES(key),
                       modes.GCM(iv),
                       default_backend()).encryptor()
    encryptor.authenticate_additional_data(aad)
    enc_value = encryptor.update(value) + encryptor.finalize()
    return "ENC[AES256_GCM,data:{value},iv:{iv}," \
        "tag:{tag},type:{valtype}]".format(
            value=b64encode(enc_value).decode('utf-8'),
            iv=b64encode(iv).decode('utf-8'),
            tag=b64encode(encryptor.tag).decode('utf-8'),
            valtype=valtype)


def get_key(tree, need_key=False):
    """Obtain a 256 bits symetric key.

    If the document contain an encrypted key, try to decrypt it using
    KMS or PGP. Otherwise, generate a new random key.

    """
    if need_key:
        # if we're here, the tree doesn't have a key yet. generate
        # one and store it in the tree
        print("please wait while a data encryption key is being generated"
              " and stored securely", file=sys.stderr)
        key = os.urandom(32)
        has_at_least_one_method = False
        if 'kms' in tree['sops']:
            i = -1
            for entry in tree['sops']['kms']:
                i += 1
                updated = encrypt_key_with_kms(key, entry)
                if 'enc' in updated and updated['enc'] != "":
                    tree['sops']['kms'][i] = updated
                    has_at_least_one_method = True
        if 'pgp' in tree['sops']:
            i = -1
            for entry in tree['sops']['pgp']:
                i += 1
                updated = encrypt_key_with_pgp(key, entry)
                if 'enc' in updated and updated['enc'] != "":
                    tree['sops']['pgp'][i] = updated
                    has_at_least_one_method = True
        if not has_at_least_one_method:
            panic("No method available to store new data key, aborting", 37)
        return key, tree
    key = get_key_from_kms(tree)
    if not (key is None):
        return key, tree
    key = get_key_from_pgp(tree)
    if not (key is None):
        return key, tree
    panic("could not retrieve a key to encrypt/decrypt the tree",
          error_code=128)


def get_key_from_kms(tree):
    """Get the key form the KMS tree leave."""
    try:
        kms_tree = tree['sops']['kms']
    except KeyError:
        return None
    i = -1
    for entry in kms_tree:
        i += 1
        try:
            enc = entry['enc']
        except KeyError:
            continue
        if 'arn' not in entry or entry['arn'] == "":
            print("KMS ARN not found, skipping entry %s" % i, file=sys.stderr)
            continue
        kms = get_aws_session_for_entry(entry)
        if kms is None:
            print("failed to initialize AWS KMS client for entry",
                  file=sys.stderr)
            continue
        try:
            kms_response = kms.decrypt(CiphertextBlob=b64decode(enc))
        except Exception as e:
            print("[warning] skipping kms %s: %s " % (entry['arn'], e),
                  file=sys.stderr)
            continue
        return kms_response['Plaintext']
    return None


def encrypt_key_with_kms(key, entry):
    """Encrypt the key using the KMS."""
    if 'arn' not in entry or entry['arn'] == "":
        print("KMS ARN not found, skipping entry", file=sys.stderr)
        return entry
    kms = get_aws_session_for_entry(entry)
    if kms is None:
        print("failed to initialize AWS KMS client for entry",
              file=sys.stderr)
        return entry
    try:
        kms_response = kms.encrypt(KeyId=entry['arn'], Plaintext=key)
    except Exception as e:
        print("failed to encrypt key using kms arn %s: %s, skipping it" %
              (entry['arn'], e), file=sys.stderr)
        return None
    entry['enc'] = b64encode(
        kms_response['CiphertextBlob']).decode('utf-8')
    entry['created_at'] = NOW
    return entry


def get_aws_session_for_entry(entry):
    """Return a boto3 session using a role if one exists in the entry"""
    # extract the region from the ARN
    # arn:aws:kms:{REGION}:...
    res = re.match('^arn:aws:kms:(.+):([0-9]+):key/(.+)$', entry['arn'])
    if res is None:
        print("Invalid ARN '%s' in entry" % entry['arn'], file=sys.stderr)
        return None
    try:
        region = res.group(1)
    except:
        print("Unable to find region from ARN '%s' in entry" %
              entry['arn'], file=sys.stderr)
        return None
    # if there are no role to assume, return the client directly
    if not ('role' in entry):
        return boto3.client('kms', region_name=region)
    # otherwise, create a client using temporary tokens that assume the role
    try:
        client = boto3.client('sts')
        role = client.assume_role(RoleArn=entry['role'],
                                  RoleSessionName='sops@'+gethostname())
    except Exception as e:
        print("Unable to switch roles: %s" % e, file=sys.stderr)
        return None
    try:
        print("Assuming AWS role '%s'" % role['AssumedRoleUser']['Arn'],
              file=sys.stderr)
        keyid = role['Credentials']['AccessKeyId']
        secretkey = role['Credentials']['SecretAccessKey']
        token = role['Credentials']['SessionToken']
        return boto3.client('kms', region_name=region,
                            aws_access_key_id=keyid,
                            aws_secret_access_key=secretkey,
                            aws_session_token=token)
    except KeyError:
        return None


def get_key_from_pgp(tree):
    """Retrieve the key from the PGP tree leave."""
    try:
        pgp_tree = tree['sops']['pgp']
    except KeyError:
        return None
    i = -1
    for entry in pgp_tree:
        i += 1
        try:
            enc = entry['enc']
        except KeyError:
            continue
        try:
            p = subprocess.Popen(['gpg', '-d'], stdout=subprocess.PIPE,
                                 stdin=subprocess.PIPE)
            key = p.communicate(input=enc.encode('utf-8'))[0]
        except Exception as e:
            print("PGP decryption failed in entry %s with error: %s" %
                  (i, e), file=sys.stderr)
            continue
        if len(key) == 32:
            return key
    return None


def encrypt_key_with_pgp(key, entry):
    """Encrypt the key using the PGP key."""
    if 'fp' not in entry or entry['fp'] == "":
        print("PGP fingerprint not found, skipping entry", file=sys.stderr)
        return entry
    fp = entry['fp']
    try:
        p = subprocess.Popen(['gpg', '--no-default-recipient', '--yes',
                              '--encrypt', '-a', '-r', fp, '--trusted-key',
                              fp[-16:], '--no-encrypt-to'],
                             stdout=subprocess.PIPE,
                             stdin=subprocess.PIPE)
        enc = p.communicate(input=key)[0]
    except Exception as e:
        print("failed to encrypt key using pgp fp %s: %s, skipping it" %
              (fp, e), file=sys.stderr)
        return None
    enc = enc.decode('utf-8')
    entry['enc'] = ruamel.yaml.scalarstring.PreservedScalarString(enc)
    entry['created_at'] = NOW
    return entry


def write_file(tree, path=None, filetype=None):
    """Write the tree content in a file using filetype format.

    Write the content of `tree` encoded using the format defined by
    `filetype` at the location `path`.
    If `path` is not defined, a tempfile is created.
    if `filetype` is not defined, tree is treated as a blob of data.

    Return the path of the file written.

    """
    if path:
        fd = open(path, "wb")
    else:
        fd = tempfile.NamedTemporaryFile(suffix="."+filetype, delete=False)
        path = fd.name

    if not isinstance(tree, dict) and not isinstance(tree, list):
        fd.write(tree.encode('utf-8'))
        fd.close()
        return path

    if filetype == "yaml":
        fd.write(ruamel.yaml.dump(tree, Dumper=ruamel.yaml.RoundTripDumper,
                                  indent=4).encode('utf-8'))
    elif filetype == "json":
        fd.write(json.dumps(tree, indent=4).encode('utf-8'))
    else:
        if 'data' in tree:
            try:
                fd.write(tree['data'].encode('utf-8'))
            except:
                fd.write(tree['data'])
        if 'sops' in tree:
            jsonstr = json.dumps(tree['sops'], sort_keys=True)
            fd.write(("SOPS=%s" % jsonstr).encode('utf-8'))

    fd.close()
    return path


def run_editor(path):
    """Open the text editor on the given file path."""
    editor = None
    if 'EDITOR' in os.environ:
        editor = os.environ['EDITOR']
    else:
        process = subprocess.Popen(["which", "vim", "nano"],
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        for line in process.stdout:
            editor = line.strip()
            break

    if editor:
        subprocess.call([editor, path])
    else:
        panic("Please define your EDITOR environment variable.", 201)
    return


def validate_syntax(path, filetype):
    """Attempt to load a file and return an exception if it fails."""
    if filetype == 'bytes':
        return True
    with open(path, "rb") as fd:
        if filetype == 'yaml':
            ruamel.yaml.load(fd, ruamel.yaml.RoundTripLoader)
        if filetype == 'json':
            json.load(fd)
    return True


def truncate_tree(tree, path):
    """ return the branch or value of a tree at the path provided """
    comps = path.split('[', -1)
    for comp in comps:
        if comp == "":
            continue
        if comp[len(comp)-1] != "]":
            panic("invalid tree path format: tree"+path, 91)
        comp = comp[0:len(comp)-1]
        comp = comp.replace('"', '', 2)
        comp = comp.replace("'", "", 2)
        if re.search(b'^\d+$', comp.encode('utf-8')):
            tree = tree[int(comp)]
        else:
            tree = tree[comp]
    return tree


def panic(msg, error_code=1):
    print("PANIC: %s" % msg, file=sys.stderr)
    sys.exit(error_code)


if __name__ == '__main__':
    main()
