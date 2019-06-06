#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import brotli
import doctest
import io
import struct

import bits
import encode
import lazy
import model
import strings
import tycheck

# The magic header for all (recent) binjs formats.
MAGIC_HEADER = b"\x89BJS\r\n\0\n"

# The version of the format. binjs-fbssdc implements context-0.1, which is
# the second released version of the format.
FORMAT_VERSION = struct.pack('b', 2)
assert struct.calcsize('b') == 1

def write(types, string_dict, ty, tree, out):
  '''Compresses ast and writes it to a byte stream.

  Note, this may modify tree. ShiftAST produces trees with numbers in
  double fields as ints. Our type-directed encoder coerces them to
  doubles. This updates the input tree, in place, with this change.

  Args:
    types: idl.TypeResolver
    string_dict: list of strings stored in external file.
    ty: the type of 'tree'.
    tree: the AST to encode.
    out: byte-oriented stream to write content to.
  '''
  # Rewrite ints in float position to floats.
  tycheck.FloatFixer(types).rewrite(ty, tree)

  # Check the AST conforms to the IDL.
  tycheck.TypeChecker(types).check_any(ty, tree)

  out.write(MAGIC_HEADER)
  out.write(FORMAT_VERSION)

  # The content is brotli-compressed
  content_no_brotli = io.BytesIO()

  # Collect the local strings
  local_strings = strings.StringCollector(types)
  local_strings.visit(ty, tree)
  local_strings.strings -= set(string_dict)
  local_strings = list(sorted(local_strings.strings))
  string_dict = local_strings + string_dict
  strings.write_dict(content_no_brotli, local_strings, with_signature=False)

  # Build probability models of the AST and serialize it.
  m = model.model_tree(types, ty, tree)
  model_writer = encode.ModelWriter(types, string_dict, content_no_brotli)
  model_writer.write(ty, m)

  # Now write the file content.
  def write_piece(ty, node, out):
    lazy_parts = lazy.LazyMemberExtractor(types)
    node = lazy_parts.replace(ty, node)

    encode.encode(types, m, out, ty, node)

    # Encode the lazy parts in memory
    lazy_encoded = []
    for _, attr, part in lazy_parts.lazies:
      buf = io.BytesIO()
      lazy_encoded.append(buf)
      write_piece(attr.resolved_ty, part, buf)

    # Write the dictionary of lazy parts, then the lazy parts
    bits.write_varint(out, len(lazy_encoded))
    for encoded_part in lazy_encoded:
      bits.write_varint(out, encoded_part.tell())
    for encoded_part in lazy_encoded:
      out.write(encoded_part.getbuffer())

  write_piece(ty, tree, content_no_brotli)

  content_compressed = brotli.compress(content_no_brotli.getvalue())
  out.write(content_compressed)


def read(types, string_dict, ty, inp):
  '''Decompresses ast from a byte stream and returns an AST.

  >>> import json
  >>> import ast, idl, strings
  >>> types = idl.parse_es6_idl()
  >>> ty_script = types.interfaces['Script']
  >>> tree_in = ast.load_test_ast('y5R7cnYctJv.js.dump')
  >>> #tree_in = ast.load_test_ast('three.min.js.dump')
  >>> string_dict = strings.prepare_dict(types, [(ty_script, tree_in)])
  >>> buf = io.BytesIO()
  >>> write(types, string_dict, ty_script, tree_in, buf)
  >>> buf.tell()
  1884
  >>> buf.seek(0)
  0
  >>> tree_out = read(types, string_dict, ty_script, buf)
  >>> #assert json.dumps(tree_in) == json.dumps(tree_out)
  >>> s_in = json.dumps(tree_in, indent=1).split('\\n')
  >>> s_out = json.dumps(tree_out, indent=1).split('\\n')
  >>> for i, (l_in, l_out) in enumerate(zip(s_in, s_out)):
  ...   if l_in != l_out:
  ...     print(f'{i:3d} {l_in}')
  ...     print(f'     {l_out}')
  ...     print('mismatch')
  ...     break

  Now try to round-trip something which uses laziness:

  >>> import opt
  >>> tree_in = opt.optimize(tree_in)
  lazified 1 functions
  >>> buf = io.BytesIO()
  >>> write(types, string_dict, ty_script, tree_in, buf)
  >>> buf.tell()
  1898
  >>> buf.seek(0)
  0
  >>> tree_out = read(types, string_dict, ty_script, buf)
  >>> assert json.dumps(tree_in) == json.dumps(tree_out)

  Now try to round-trip something which misses the dictionary:

  >>> del string_dict[-10:-3]
  >>> buf = io.BytesIO()
  >>> write(types, string_dict, ty_script, tree_in, buf)
  >>> buf.tell()
  1934
  >>> buf.seek(0)
  0
  >>> tree_out = read(types, string_dict, ty_script, buf)
  >>> assert json.dumps(tree_in) == json.dumps(tree_out)
  '''

  # Check that we're attempting to decompress a file with the right type.
  maybe_magic_header = inp.read(len(MAGIC_HEADER))
  assert maybe_magic_header == MAGIC_HEADER # FIXME: Replace this with a nicer error message.

  maybe_format_version = inp.read(len(FORMAT_VERSION))
  assert maybe_format_version == FORMAT_VERSION # FIXME: Replace this with a nicer error message.

  # Read and decompress the content
  content_brotli_compressed = inp.read()
  content_inp = io.BytesIO(brotli.decompress(content_brotli_compressed))

  local_strings = strings.read_dict(content_inp, with_signature=False)
  string_dict = local_strings + string_dict

  # Read the probability models
  model_reader = encode.ModelReader(types, string_dict, content_inp)
  m = model_reader.read(ty)

  def read_piece(ty, inp):
    tree = encode.decode(types, m, ty, inp)

    # Read the dictionary of lazy parts
    # TODO: We don't need this; it is implicit in the tree we just read.
    num_lazy_parts = bits.read_varint(inp)
    lazy_offsets = [0]
    for _ in range(num_lazy_parts):
      lazy_size = bits.read_varint(inp)
      lazy_offsets.append(lazy_offsets[-1] + lazy_size)
    lazy_offsets = list(map(lambda offset: offset + inp.tell(), lazy_offsets))

    def restore_lazy_part(ty, attr, index):
      inp.seek(lazy_offsets[index])
      part = read_piece(attr.resolved_ty, inp)
      assert inp.tell() == lazy_offsets[index + 1], f'{inp.tell()}, {lazy_offsets[index + 1]}'
      return part

    restorer = lazy.LazyMemberRestorer(types, restore_lazy_part)
    tree = restorer.replace(ty, tree)
    inp.seek(lazy_offsets[-1])
    return tree

  tree = read_piece(ty, content_inp)
  type_checker = tycheck.TypeChecker(types)
  type_checker.check_any(ty, tree)
  return tree


if __name__ == '__main__':
  doctest.testmod()
