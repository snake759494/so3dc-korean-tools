import csv, sys, io, os, struct
if __name__ == '__main__':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# Global so3mclib 1.72 atlas, transcribed from
# work/kanji_deep/mclib_172_render/glyph_contact_indexed.png
# glyph code (base-301 containers) = atlas_index + 1
G = []
G += list("0123456789")            # 000-009
G += list("-.'")                   # 00A-00C
G += list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")  # 00D-026
G += list("abcdefghijklmnopqrstuvwxyz")  # 027-040
G += list("を")                     # 041
G += list("ぁぃぅぇぉゃゅょっゎ")   # 042-04B small hiragana
G += list("あいうえ")               # 04C-04F
G += list("おかきくけこさしすせそたちつてと")  # 050-05F
G += list("なにぬねのはひふへほまみむめもや")  # 060-06F
G += list("ゆよらりるれろわん")     # 070-078
G += list("がぎぐげござじ")         # 079-07F
G += list("ずぜぞだぢづでどばびぶべぼぱぴぷ")  # 080-08F
G += list("ぺぽ")                   # 090-091
G += list("ヲ")                     # 092
G += list("ァィゥェォャュョッ")     # 093-09B small katakana
G += list("ー")                     # 09C
G += list("アイウ")                 # 09D-09F
G += list("エオカキクケコサシスセソタチツテ")  # 0A0-0AF
G += list("トナニヌネノハヒフヘホマミムメモ")  # 0B0-0BF
G += list("ヤユヨラリルレロワンヴガギグゲゴ")  # 0C0-0CF
G += list("ザジズゼゾダヂヅデドバビブベボパ")  # 0D0-0DF
G += list("ピプペポヵヶヮ")         # 0E0-0E6
G += list("  ")                    # 0E7-0E8 blank
G += list("、ヽ°゜・・?")           # 0E9-0EF
G += list("?!!))〉》]]}}」」』$%")  # 0F0-0FF
G += list("&,:;=|((<<[[{{「「")     # 100-10F
G += list("『#@″*+/^_`~…♪★♥―")     # 110-11F
G += list("↑↓←→")                 # 120-123
assert len(G) == 292, len(G)

def decode_segment(b, base=301):
    """Decode mclib message bytes to text. Local glyphs -> 〓, unresolved control -> stop."""
    out = []
    i = 0
    n = len(b)
    complete = True
    nlocal = 0
    while i < n:
        c = b[i]
        if c == 0:
            break
        if c >= 0x80:
            if i + 1 >= n:
                complete = False
                break
            c2 = b[i+1]
            if c2 >= 0x80:
                # control pair
                if c == 0x80 and c2 == 0x80:
                    out.append('\\n')
                    i += 2
                    continue
                if c == 0x8A and c2 == 0x80:
                    i += 6  # skip float32 scale
                    continue
                out.append(f'⟂{c:02X}{c2:02X}')
                complete = False
                break
            code = (c & 0x7F) | (c2 << 7)
            i += 2
        else:
            code = c
            i += 1
        if code < base:
            if 1 <= code <= 292:
                out.append(G[code - 1])
            else:
                out.append(f'⟦{code}⟧')
        else:
            out.append('〓')
            nlocal += 1
    return ''.join(out), complete, nlocal

if __name__ == '__main__':
    _ws = os.environ.get(
        "SO3_WS", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    p = os.path.join(_ws, "work", "mclib_all_decode", "unique_exact_segments.csv")
    outp = sys.argv[1] if len(sys.argv) > 1 else 'decoded_segments.tsv'
    root = os.path.join(_ws, "work", "full_unpack", "disc1")
    n = 0
    with open(outp, 'w', encoding='utf-8') as out:
        out.write("archive\tstream\tmsgid\toccurrences\tbytes\tcomplete\tnlocal\ttext\tpath\n")
        for row in csv.DictReader(open(p, encoding='utf-8', newline='')):
            hx = row['representative_hex']
            trunc = row['representative_hex_truncated'] not in ('0', '', 'False', 'false')
            if trunc:
                # re-read from file
                path = row['representative_path']
                if not os.path.isabs(path):
                    path = os.path.join(root, path)
                try:
                    data = open(path, 'rb').read()
                    off = int(row['representative_offset'])
                    seglen = int(row['segment_bytes'])
                    # offset is relative to text blob; but we don't know blob start here.
                    # The catalog gives absolute? Try: representative_offset is blob-relative.
                    # Fallback: decode the truncated hex prefix instead.
                    b = bytes.fromhex(hx)
                except Exception:
                    b = bytes.fromhex(hx)
            else:
                b = bytes.fromhex(hx)
            txt, complete, nlocal = decode_segment(b)
            txt = txt.replace('\t', ' ')
            out.write(f"{row['representative_archive_id']}\t{row['representative_stream_id']}\t{row['representative_message_id']}\t{row['all_mapping_occurrences']}\t{row['segment_bytes']}\t{int(complete)}\t{nlocal}\t{txt}\t{row['representative_path']}\n")
            n += 1
    print("decoded", n, "segments ->", outp)
