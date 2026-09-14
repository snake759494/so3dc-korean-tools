using System.Buffers.Binary;
using System.Globalization;
using System.Text;
using System.Text.Json;

const int Sector = 0x800;
const int EntryCount = 0x1800;
const long TableOffset = 0x200000;
const uint Seed = 0x13578642;
const uint Signature = 0x27D51556;

if (args.Length < 2 || args.Contains("--help"))
{
    Console.WriteLine("SO3 hidden archive extractor\n" +
        "Usage: So3Unpack ISO OUTPUT [--raw-mode full|none] [--decoded-mode all|priority|none] " +
        "[--max-depth N] [--resume] [--no-json]\n\n" +
        "The defaults are --raw-mode full --decoded-mode all --max-depth 3.\n" +
        "Use --raw-mode none to avoid the 4.7 GiB raw copy, or --decoded-mode priority to save only fonts, FIS, ELF and likely text resources.");
    return 2;
}

string isoPath = Path.GetFullPath(args[0]);
string outputRoot = Path.GetFullPath(args[1]);
string rawMode = Option(args, "--raw-mode", "full");
string decodedMode = Option(args, "--decoded-mode", "all");
int maxDepth = int.Parse(Option(args, "--max-depth", "3"), CultureInfo.InvariantCulture);
bool resume = args.Contains("--resume");
bool writeJson = !args.Contains("--no-json");
if (!File.Exists(isoPath)) throw new FileNotFoundException("ISO not found", isoPath);
if (!(rawMode is "full" or "none")) throw new ArgumentException("--raw-mode must be full or none");
if (!(decodedMode is "all" or "priority" or "none")) throw new ArgumentException("--decoded-mode must be all, priority or none");

Directory.CreateDirectory(outputRoot);
Directory.CreateDirectory(Path.Combine(outputRoot, "raw"));
Directory.CreateDirectory(Path.Combine(outputRoot, "decoded"));
Directory.CreateDirectory(Path.Combine(outputRoot, "manifests"));

var archiveRows = new List<ArchiveRow>(EntryCount);
var streamRows = new List<StreamRow>(70000);
var packRows = new List<PackRow>();
var priorityRows = new List<PriorityRow>();

using var iso = new FileStream(isoPath, FileMode.Open, FileAccess.Read, FileShare.Read, 1 << 20, FileOptions.RandomAccess);
uint[] table = ReadAndDecodeIndex(iso);
WriteDecodedIndex(Path.Combine(outputRoot, "index_decoded.bin"), table);

long rawWritten = 0, decodedWritten = 0, decompressedTotal = 0;
int globalStreamId = 0;
for (int id = 0; id < EntryCount; id++)
{
    uint lba = table[id];
    uint sectors = table[EntryCount + id];
    uint third = table[EntryCount * 2 + id];
    long byteLength = (long)sectors * Sector;
    long isoOffset = (long)lba * Sector;
    if (sectors == 0)
    {
        archiveRows.Add(new ArchiveRow(id, lba, sectors, 0, third, isoOffset, "empty", "", 0));
        continue;
    }
    if (byteLength > int.MaxValue) throw new InvalidDataException($"Entry {id} is too large for this build");
    byte[] data = new byte[(int)byteLength];
    ReadExact(iso, isoOffset, data);
    string kind = ClassifyArchive(data);
    string extension = ArchiveExtension(kind);
    string rawRelative = Path.Combine("raw", $"{id:D4}{extension}");
    if (rawMode == "full")
    {
        string rawPath = Path.Combine(outputRoot, rawRelative);
        WriteIfNeeded(rawPath, data, resume);
        rawWritten += data.Length;
    }

    var candidates = FindSlz(data, 0, data.Length);
    var localOffsetToId = new Dictionary<int, int>();
    foreach (var candidate in candidates)
    {
        int sid = globalStreamId++;
        localOffsetToId[candidate.Offset] = sid;
        byte[] decoded;
        string error = "";
        try { decoded = DecompressSlz(data, candidate); }
        catch (Exception ex)
        {
            decoded = Array.Empty<byte>();
            error = ex.Message;
        }
        int parentId = -1;
        string sourceKind = "root";
        foreach (var previous in candidates)
        {
            if (previous.Offset >= candidate.Offset) break;
            if (previous.NextRel > 1 && previous.Offset + previous.NextRel == candidate.Offset && localOffsetToId.TryGetValue(previous.Offset, out parentId))
            {
                sourceKind = "chain";
                break;
            }
        }
        var row = ProcessDecoded(
            archiveId: id, streamId: sid, parentStreamId: parentId, depth: 0,
            sourceKind: sourceKind, sourceOffset: candidate.Offset,
            absoluteIsoOffset: isoOffset + candidate.Offset,
            candidate: candidate, decoded: decoded, error: error,
            outputRoot: outputRoot, decodedMode: decodedMode, resume: resume,
            maxDepth: maxDepth, streamRows: streamRows, packRows: packRows,
            priorityRows: priorityRows, globalStreamId: ref globalStreamId,
            decodedWritten: ref decodedWritten, decompressedTotal: ref decompressedTotal);
    }
    archiveRows.Add(new ArchiveRow(id, lba, sectors, byteLength, third, isoOffset, kind,
        rawMode == "full" ? Normalize(rawRelative) : "", candidates.Count));
    if ((id + 1) % 100 == 0 || id + 1 == EntryCount)
    {
        Console.WriteLine($"entries={id + 1}/{EntryCount} streams={streamRows.Count} raw={rawWritten / 1073741824.0:F2}GiB decoded={decodedWritten / 1073741824.0:F2}GiB");
    }
}

WriteCsv(Path.Combine(outputRoot, "manifests", "archive_manifest.csv"), ArchiveRow.Header, archiveRows.Select(x => x.Csv()));
WriteCsv(Path.Combine(outputRoot, "manifests", "stream_manifest.csv"), StreamRow.Header, streamRows.Select(x => x.Csv()));
WriteCsv(Path.Combine(outputRoot, "manifests", "pack_manifest.csv"), PackRow.Header, packRows.Select(x => x.Csv()));
WriteCsv(Path.Combine(outputRoot, "manifests", "priority_candidates.csv"), PriorityRow.Header, priorityRows.Select(x => x.Csv()));
if (writeJson)
{
    var manifest = new
    {
        format = "tri-Ace SO3 PS2 hidden archive",
        iso = isoPath,
        table_offset = TableOffset,
        seed = $"0x{Seed:X8}",
        entry_count = EntryCount,
        raw_mode = rawMode,
        decoded_mode = decodedMode,
        totals = new { raw_bytes = rawWritten, decompressed_bytes = decompressedTotal, decoded_saved_bytes = decodedWritten, streams = streamRows.Count, pack_entries = packRows.Count },
        archives = archiveRows,
        streams = streamRows,
        pack_entries = packRows,
        priority_candidates = priorityRows
    };
    File.WriteAllText(Path.Combine(outputRoot, "manifests", "manifest.json"),
        JsonSerializer.Serialize(manifest, new JsonSerializerOptions { WriteIndented = true }), new UTF8Encoding(false));
}
Console.WriteLine($"DONE archives={archiveRows.Count} streams={streamRows.Count} decompressed={decompressedTotal / 1073741824.0:F3}GiB saved={decodedWritten / 1073741824.0:F3}GiB");
return 0;

static string Option(string[] arguments, string name, string fallback)
{
    int i = Array.IndexOf(arguments, name);
    if (i < 0) return fallback;
    if (i + 1 >= arguments.Length) throw new ArgumentException($"Missing value for {name}");
    return arguments[i + 1];
}

static uint[] ReadAndDecodeIndex(FileStream iso)
{
    byte[] encoded = new byte[EntryCount * 3 * 4];
    ReadExact(iso, TableOffset, encoded);
    var table = new uint[EntryCount * 3];
    for (int i = 0; i < table.Length; i++) table[i] = BinaryPrimitives.ReadUInt32LittleEndian(encoded.AsSpan(i * 4, 4));
    if (table[0] != Signature) throw new InvalidDataException($"SO3 index signature not found at 0x{TableOffset:X}: got 0x{table[0]:X8}");
    uint key = Seed;
    unchecked
    {
        for (int i = 0; i < EntryCount; i++)
        {
            table[i] ^= key; key ^= key << 1;
            table[EntryCount + i] ^= key; key ^= ~Seed;
            table[EntryCount * 2 + i] ^= key; key ^= (key << 2) ^ Seed;
        }
    }
    table[0] = (uint)(TableOffset / Sector);
    return table;
}

static void WriteDecodedIndex(string path, uint[] table)
{
    byte[] bytes = new byte[table.Length * 4];
    for (int i = 0; i < table.Length; i++) BinaryPrimitives.WriteUInt32LittleEndian(bytes.AsSpan(i * 4, 4), table[i]);
    File.WriteAllBytes(path, bytes);
}

static void ReadExact(FileStream stream, long offset, byte[] destination)
{
    stream.Position = offset;
    int got = 0;
    while (got < destination.Length)
    {
        int n = stream.Read(destination, got, destination.Length - got);
        if (n == 0) throw new EndOfStreamException($"Short ISO read at 0x{offset + got:X}");
        got += n;
    }
}

static string ClassifyArchive(byte[] d)
{
    if (d.Length < 4) return "bin";
    uint h = U32(d, 0);
    return h switch
    {
        0x464C457F => "elf", 0x005A4C53 or 0x015A4C53 or 0x025A4C53 or 0x035A4C53 => "slz",
        0x00454C53 or 0x01454C53 or 0x02454C53 or 0x03454C53 => "sle", 0x00534C5A => "zls",
        0x57514553 => "seq", 0x4B434150 => "pac", 0x73696854 => "txt", 0x00594D44 => "dmy",
        0x00000020 => "020", 0x6D336F73 or 0x7370636D => "mc", 0x27D51556 or 0x516F6699 => "idx",
        0x67225277 => "unk", 0x73646F4B => "kod", 0x00504352 => "rcp", 0x00534946 => "fis",
        0x00435243 => "crc", 0 => ClassifyZero(d), _ => ClassifyPacked(d)
    };
}

static string ClassifyZero(byte[] d)
{
    if (d.Length >= 12 && U32(d, 4) == 0 && U32(d, 8) == 0x10) return "010";
    if (d.Length >= 12 && U32(d, 4) == 0 && U32(d, 8) == 0) return "000";
    if (d.Length >= 0x20)
    {
        uint a = U32(d, 4), b = U32(d, 8);
        if (16L * (a + 1L) == b && U32(d, 0x1C) == b) return "pk1";
    }
    return "bin";
}

static string ClassifyPacked(byte[] d)
{
    uint h = U32(d, 0);
    if (h > 0 && h < 0x100 && 4L + h * 4L + 2 <= d.Length)
    {
        bool ok = true;
        uint a = 0, b = 0, c = 0;
        for (int j = 0; j < h; j++)
        {
            int p = 4 + j * 4;
            a = U16(d, p); b = U16(d, p + 2); c = U16(d, p + 4);
            if (a + b != c) { ok = false; break; }
        }
        if (ok && (a + b) * (long)Sector == d.Length) return "pk2";
    }
    if (d.Length >= 0x18 && U32(d, 0x14) == d.Length) return "pk3";
    return "bin";
}

static string ArchiveExtension(string kind) => kind == "bin" ? ".bin" : "." + kind;

static List<SlzCandidate> FindSlz(byte[] data, int start, int length)
{
    var result = new List<SlzCandidate>();
    int end = Math.Min(data.Length, start + length);
    for (int p = start; p + 16 <= end; p++)
    {
        if (data[p] != (byte)'S' || data[p + 1] != (byte)'L' || data[p + 2] != (byte)'Z') continue;
        int mode = data[p + 3];
        if (mode is < 0 or > 3) continue;
        uint compressed = U32(data, p + 4), unpacked = U32(data, p + 8), next = U32(data, p + 12);
        if (compressed == 0 || unpacked == 0 || compressed > 0x10000000 || unpacked > 0x20000000) continue;
        if ((long)p + 16 + compressed > end) continue;
        if (mode == 0 && compressed != unpacked) continue;
        if (mode != 0 && compressed > unpacked * 2L + 0x100) continue;
        if (next != 0 && (next < 16 + compressed || (next & 3) != 0 || (long)p + next >= end)) continue;
        result.Add(new SlzCandidate(p, mode, (int)compressed, (int)unpacked, (int)next));
        p += 2;
    }
    return result;
}

static byte[] DecompressSlz(byte[] source, SlzCandidate c)
{
    if (c.Mode == 0) return source.AsSpan(c.Offset + 16, c.Unpacked).ToArray();
    int src = c.Offset + 16, srcEnd = src + c.Compressed, dst = 0;
    byte[] output = new byte[c.Unpacked];
    uint flags = 0;
    while (dst < output.Length)
    {
        flags >>= 1;
        if (flags <= 0xFFFF)
        {
            if (src >= srcEnd) break;
            flags = 0x00FF0000u | source[src++];
            if (c.Mode == 3)
            {
                if (src >= srcEnd) break;
                flags |= 0xFF000000u | ((uint)source[src++] << 8);
            }
        }
        if ((flags & 1) != 0)
        {
            int unit = c.Mode == 3 ? 2 : 1;
            for (int j = 0; j < unit && dst < output.Length && src < srcEnd; j++) output[dst++] = source[src++];
        }
        else
        {
            if (src + 2 > srcEnd) break;
            int pos = source[src++], count = source[src++];
            if (c.Mode == 2 && count >= 0xF0)
            {
                if (count > 0xF0) count = (count & 0x0F) + 3;
                else
                {
                    count = pos + 0x13;
                    if (src >= srcEnd) break;
                    pos = source[src++];
                }
                int n = Math.Min(count, output.Length - dst);
                output.AsSpan(dst, n).Fill((byte)pos);
                dst += n;
            }
            else
            {
                pos |= (count & 0x0F) << 8;
                count = (count >> 4) + 3;
                if (c.Mode == 3) { count = (count - 1) << 1; pos <<= 1; }
                if (pos == 0 || pos > dst) break;
                for (int j = 0; j < count && dst < output.Length; j++) { output[dst] = output[dst - pos]; dst++; }
            }
        }
    }
    if (dst != output.Length) throw new InvalidDataException($"short SLZ output: got 0x{dst:X}, expected 0x{output.Length:X}");
    return output;
}

static StreamRow ProcessDecoded(int archiveId, int streamId, int parentStreamId, int depth,
    string sourceKind, int sourceOffset, long absoluteIsoOffset, SlzCandidate candidate,
    byte[] decoded, string error, string outputRoot, string decodedMode, bool resume, int maxDepth,
    List<StreamRow> streamRows, List<PackRow> packRows, List<PriorityRow> priorityRows,
    ref int globalStreamId, ref long decodedWritten, ref long decompressedTotal)
{
    decompressedTotal += decoded.Length;
    string magicHex = Convert.ToHexString(decoded.AsSpan(0, Math.Min(16, decoded.Length))).ToLowerInvariant();
    string magicText = MagicText(decoded);
    string fisName = GetFisName(decoded);
    string extension = DecodedExtension(decoded, magicText);
    var sjis = MeasureSjis(decoded);
    string priority = PriorityReason(decoded, magicText, fisName, sjis.Longest, sjis.Pairs);
    bool save = decodedMode == "all" || (decodedMode == "priority" && priority.Length > 0);
    string relative = "";
    if (save && decoded.Length > 0)
    {
        string label = fisName.Length > 0 ? "_" + SafeName(fisName) : "";
        relative = Path.Combine("decoded", $"{archiveId:D4}", $"s{streamId:D6}_d{depth}_o{sourceOffset:X8}{label}{extension}");
        WriteIfNeeded(Path.Combine(outputRoot, relative), decoded, resume);
        decodedWritten += decoded.Length;
    }
    string packStatus = "";
    int packEntryCount = 0;
    if (StartsWithAscii(decoded, "PACK"))
    {
        var parsed = ParsePack(decoded);
        packStatus = parsed.Status;
        packEntryCount = parsed.Entries.Count;
        foreach (var pe in parsed.Entries)
        {
            string subRel = Path.Combine("decoded", $"{archiveId:D4}", $"s{streamId:D6}_pack", $"{pe.Index:D4}_{pe.Id:X8}.bin");
            if (decodedMode == "all")
            {
                byte[] member = decoded.AsSpan(pe.Offset, pe.Size).ToArray();
                WriteIfNeeded(Path.Combine(outputRoot, subRel), member, resume);
                decodedWritten += member.Length;
            }
            packRows.Add(new PackRow(archiveId, streamId, pe.Index, pe.Id, pe.Offset, pe.Size,
                decodedMode == "all" ? Normalize(subRel) : "", parsed.Endian));
        }
    }
    var row = new StreamRow(archiveId, streamId, parentStreamId, depth, sourceKind, sourceOffset,
        absoluteIsoOffset, candidate.Mode, candidate.Compressed, candidate.Unpacked, candidate.NextRel,
        magicHex, magicText, fisName, extension, Normalize(relative), packStatus, packEntryCount,
        sjis.Longest, sjis.Pairs, priority, error);
    streamRows.Add(row);
    if (priority.Length > 0) priorityRows.Add(new PriorityRow(archiveId, streamId, magicText, fisName, decoded.Length, priority, Normalize(relative)));

    if (decoded.Length > 0 && depth < maxDepth)
    {
        foreach (var nested in FindSlz(decoded, 0, decoded.Length))
        {
            byte[] nestedDecoded;
            string nestedError = "";
            try { nestedDecoded = DecompressSlz(decoded, nested); }
            catch (Exception ex) { nestedDecoded = Array.Empty<byte>(); nestedError = ex.Message; }
            int nestedId = globalStreamId++;
            ProcessDecoded(archiveId, nestedId, streamId, depth + 1, "nested", nested.Offset, -1,
                nested, nestedDecoded, nestedError, outputRoot, decodedMode, resume, maxDepth,
                streamRows, packRows, priorityRows, ref globalStreamId, ref decodedWritten, ref decompressedTotal);
        }
    }
    return row;
}

static (string Status, string Endian, List<PackEntry> Entries) ParsePack(byte[] d)
{
    if (d.Length < 16) return ("short", "", new());
    foreach (bool big in new[] { false, true })
    {
        uint count = Read32(d, 8, big), packSize = Read32(d, 12, big);
        if (count == 0 || count > 100000 || 16L + count * 16L > d.Length) continue;
        var entries = new List<PackEntry>((int)count);
        bool ok = true;
        int dataStart = checked(16 + (int)count * 16), nonEmpty = 0;
        for (int i = 0; i < count; i++)
        {
            int p = 16 + i * 16;
            uint id = Read32(d, p + 4, big), size = Read32(d, p + 8, big), offset = Read32(d, p + 12, big);
            if (size > int.MaxValue || offset > int.MaxValue || (long)offset + size > d.Length || (size > 0 && offset < dataStart)) { ok = false; break; }
            if (size > 0) nonEmpty++;
            entries.Add(new PackEntry(i, id, (int)offset, (int)size));
        }
        if (ok && nonEmpty > 0 && (packSize == 0 || packSize <= d.Length)) return ("parsed", big ? "big" : "little", entries);
    }
    // SO3 also uses PACK as an internal graphics/animation structure.  Those
    // files do not satisfy the QuickBMS 16-byte subentry table invariants.
    return ("asset_header_not_subarchive", "", new());
}

static (int Longest, int Pairs) MeasureSjis(byte[] d)
{
    int limit = Math.Min(d.Length, 2 << 20), longest = 0, pairsInBest = 0, run = 0, pairs = 0;
    for (int i = 0; i < limit;)
    {
        byte b = d[i];
        bool lead = (b >= 0x81 && b <= 0x9F) || (b >= 0xE0 && b <= 0xEF);
        if (lead && i + 1 < limit && ((d[i + 1] >= 0x40 && d[i + 1] <= 0x7E) || (d[i + 1] >= 0x80 && d[i + 1] <= 0xFC)))
        { run += 2; pairs++; i += 2; }
        else if ((b >= 0x20 && b <= 0x7E) || b is 0x09 or 0x0A or 0x0D || (b >= 0xA1 && b <= 0xDF))
        { run++; i++; }
        else
        {
            if (run > longest) { longest = run; pairsInBest = pairs; }
            run = 0; pairs = 0; i++;
        }
    }
    if (run > longest) { longest = run; pairsInBest = pairs; }
    return (longest, pairsInBest);
}

static string PriorityReason(byte[] d, string magic, string fis, int sjisLongest, int sjisPairs)
{
    var r = new List<string>();
    if (d.AsSpan().StartsWith(new byte[] { 0x7F, (byte)'E', (byte)'L', (byte)'F' })) r.Add("ELF_overlay");
    if (magic == "FIS") r.Add("FIS_image");
    // SHI is a generic FIS resource name as well as the small UI font name.
    // The confirmed font atlases have exact payload sizes; larger SHI images
    // (notably the 102 nested resources in archive 3244) are not fonts.
    if ((fis.Equals("SHI", StringComparison.OrdinalIgnoreCase) && d.Length == 34304) ||
        (fis.StartsWith("ANKF", StringComparison.OrdinalIgnoreCase) && d.Length == 11776)) r.Add("known_font");
    if (magic == "This") r.Add("text_container");
    if (sjisLongest >= 48 && sjisPairs >= 8) r.Add("Shift_JIS_candidate");
    if (magic.StartsWith("so3mclib", StringComparison.Ordinal)) r.Add("runtime_module");
    return string.Join(';', r);
}

static string MagicText(byte[] d)
{
    if (d.Length >= 4 && d[0] == 0x7F && d[1] == 'E' && d[2] == 'L' && d[3] == 'F') return "ELF";
    int n = Math.Min(16, d.Length), end = 0;
    while (end < n && d[end] != 0 && d[end] >= 0x20 && d[end] < 0x7F) end++;
    return end == 0 ? "" : Encoding.ASCII.GetString(d, 0, end);
}

static string GetFisName(byte[] d)
{
    if (d.Length < 21 || !StartsWithAscii(d, "FIS\0")) return "";
    int start = 20, end = start;
    while (end < d.Length && end < start + 48 && d[end] >= 0x20 && d[end] < 0x7F) end++;
    return end == start ? "" : Encoding.ASCII.GetString(d, start, end - start);
}

static string DecodedExtension(byte[] d, string magic)
{
    if (magic == "ELF") return ".elf";
    return magic switch
    {
        "FIS" => ".fis", "PACK" => ".pack", "FAS" => ".fas", "FPS" => ".fps", "RTA" => ".rta",
        "DMM" => ".dmm", "RMAC" or "RMAC " => ".rmac", "TGILP" => ".tgilp", "so3mclib 1.75" => ".mclib",
        "This" => ".txt", _ => ".bin"
    };
}

static string SafeName(string s)
{
    var b = new StringBuilder();
    foreach (char c in s) b.Append(char.IsLetterOrDigit(c) || c is '-' or '_' ? c : '_');
    return b.ToString().Trim('_');
}

static void WriteIfNeeded(string path, byte[] data, bool resume)
{
    Directory.CreateDirectory(Path.GetDirectoryName(path)!);
    if (resume && File.Exists(path) && new FileInfo(path).Length == data.Length) return;
    File.WriteAllBytes(path, data);
}

static void WriteCsv(string path, string header, IEnumerable<string> lines)
{
    using var w = new StreamWriter(path, false, new UTF8Encoding(false));
    w.WriteLine(header);
    foreach (string line in lines) w.WriteLine(line);
}

static string Normalize(string path) => path.Replace('\\', '/');
static bool StartsWithAscii(byte[] d, string text)
{
    if (d.Length < text.Length) return false;
    for (int i = 0; i < text.Length; i++) if (d[i] != (byte)text[i]) return false;
    return true;
}
static ushort U16(byte[] d, int p) => BinaryPrimitives.ReadUInt16LittleEndian(d.AsSpan(p, 2));
static uint U32(byte[] d, int p) => BinaryPrimitives.ReadUInt32LittleEndian(d.AsSpan(p, 4));
static uint Read32(byte[] d, int p, bool big) => big ? BinaryPrimitives.ReadUInt32BigEndian(d.AsSpan(p, 4)) : U32(d, p);

record ArchiveRow(int Id, uint Lba, uint Sectors, long Bytes, uint Third, long IsoOffset, string Kind, string RawPath, int SlzStreams)
{
    public const string Header = "id,lba,sectors,bytes,third,iso_offset,kind,raw_path,slz_streams";
    public string Csv() => CsvUtil.Row(Id, Lba, Sectors, Bytes, Third, IsoOffset, Kind, RawPath, SlzStreams);
}
record StreamRow(int ArchiveId, int StreamId, int ParentStreamId, int Depth, string SourceKind, int SourceOffset,
    long IsoOffset, int Mode, int Compressed, int Unpacked, int NextRel, string MagicHex, string MagicText,
    string FisName, string Extension, string Path, string PackStatus, int PackEntries, int SjisLongest,
    int SjisPairs, string PriorityReason, string Error)
{
    public const string Header = "archive_id,stream_id,parent_stream_id,depth,source_kind,source_offset,iso_offset,mode,compressed,unpacked,next_rel,magic_hex,magic_text,fis_name,extension,path,pack_status,pack_entries,sjis_longest,sjis_pairs,priority_reason,error";
    public string Csv() => CsvUtil.Row(ArchiveId, StreamId, ParentStreamId, Depth, SourceKind, SourceOffset, IsoOffset,
        Mode, Compressed, Unpacked, NextRel, MagicHex, MagicText, FisName, Extension, Path, PackStatus, PackEntries,
        SjisLongest, SjisPairs, PriorityReason, Error);
}
record PackRow(int ArchiveId, int StreamId, int EntryIndex, uint FileId, int Offset, int Size, string Path, string Endian)
{
    public const string Header = "archive_id,stream_id,entry_index,file_id,offset,size,path,endian";
    public string Csv() => CsvUtil.Row(ArchiveId, StreamId, EntryIndex, FileId, Offset, Size, Path, Endian);
}
record PriorityRow(int ArchiveId, int StreamId, string MagicText, string FisName, int Size, string Reason, string Path)
{
    public const string Header = "archive_id,stream_id,magic_text,fis_name,size,reason,path";
    public string Csv() => CsvUtil.Row(ArchiveId, StreamId, MagicText, FisName, Size, Reason, Path);
}
record SlzCandidate(int Offset, int Mode, int Compressed, int Unpacked, int NextRel);
record PackEntry(int Index, uint Id, int Offset, int Size);

static class CsvUtil
{
    public static string Row(params object?[] values) => string.Join(',', values.Select(v =>
    {
        string s = Convert.ToString(v, CultureInfo.InvariantCulture) ?? "";
        return s.IndexOfAny(new[] { ',', '"', '\r', '\n' }) >= 0 ? '"' + s.Replace("\"", "\"\"") + '"' : s;
    }));
}
