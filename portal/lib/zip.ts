// Tiny store-only (no compression) ZIP writer plus helpers that read a dropped
// folder or a folder picked with <input webkitdirectory> into {path, data}.
// No ZIP64: at most 65535 entries and 4 GiB in total.

export type ZipFile = { path: string; data: Uint8Array };

let crcTable: Uint32Array | null = null;

function table() {
  if (crcTable) return crcTable;
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  crcTable = t;
  return t;
}

export function crc32(data: Uint8Array): number {
  const t = table();
  let c = 0xffffffff;
  for (let i = 0; i < data.length; i++) c = t[(c ^ data[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function dosDateTime(d: Date) {
  const year = Math.max(1980, d.getFullYear());
  const time = (d.getHours() << 11) | (d.getMinutes() << 5) | (d.getSeconds() >> 1);
  const date = ((year - 1980) << 9) | ((d.getMonth() + 1) << 5) | d.getDate();
  return { time, date };
}

/** Build a ZIP archive (stored, not compressed). Names are UTF-8 (flag bit 11). */
export function createZip(files: ZipFile[]): Blob {
  if (files.length > 0xffff) throw new Error("Too many files for a ZIP archive");
  const enc = new TextEncoder();
  const { time, date } = dosDateTime(new Date());
  const parts: Uint8Array[] = [];
  const central: Uint8Array[] = [];
  let offset = 0;

  for (const f of files) {
    const name = enc.encode(f.path.replace(/\\/g, "/").replace(/^\/+/, ""));
    const crc = crc32(f.data);
    const size = f.data.length;
    if (size > 0xffffffff || offset > 0xffffffff) throw new Error("Archive too large (4 GiB limit)");

    const local = new DataView(new ArrayBuffer(30));
    local.setUint32(0, 0x04034b50, true);
    local.setUint16(4, 20, true); // version needed
    local.setUint16(6, 0x0800, true); // flags: UTF-8 names
    local.setUint16(8, 0, true); // method: stored
    local.setUint16(10, time, true);
    local.setUint16(12, date, true);
    local.setUint32(14, crc, true);
    local.setUint32(18, size, true);
    local.setUint32(22, size, true);
    local.setUint16(26, name.length, true);
    local.setUint16(28, 0, true);
    parts.push(new Uint8Array(local.buffer), name, f.data);

    const cd = new DataView(new ArrayBuffer(46));
    cd.setUint32(0, 0x02014b50, true);
    cd.setUint16(4, 20, true); // version made by
    cd.setUint16(6, 20, true); // version needed
    cd.setUint16(8, 0x0800, true);
    cd.setUint16(10, 0, true);
    cd.setUint16(12, time, true);
    cd.setUint16(14, date, true);
    cd.setUint32(16, crc, true);
    cd.setUint32(20, size, true);
    cd.setUint32(24, size, true);
    cd.setUint16(28, name.length, true);
    // extra, comment, disk number, internal and external attributes stay 0
    cd.setUint32(42, offset, true);
    const rec = new Uint8Array(46 + name.length);
    rec.set(new Uint8Array(cd.buffer), 0);
    rec.set(name, 46);
    central.push(rec);

    offset += 30 + name.length + size;
  }

  const cdSize = central.reduce((n, r) => n + r.length, 0);
  const end = new DataView(new ArrayBuffer(22));
  end.setUint32(0, 0x06054b50, true);
  end.setUint16(8, files.length, true);
  end.setUint16(10, files.length, true);
  end.setUint32(12, cdSize, true);
  end.setUint32(16, offset, true);
  parts.push(...central, new Uint8Array(end.buffer));
  return new Blob(parts as BlobPart[], { type: "application/zip" });
}

async function readBytes(file: File): Promise<Uint8Array> {
  return new Uint8Array(await file.arrayBuffer());
}

/** Files from <input type=file> (webkitRelativePath keeps folder structure). */
export async function filesFromInput(list: FileList | File[]): Promise<ZipFile[]> {
  const files = Array.from(list);
  return Promise.all(
    files.map(async (f) => ({
      path: (f as File & { webkitRelativePath?: string }).webkitRelativePath || f.name,
      data: await readBytes(f),
    })),
  );
}

type FsEntry = {
  isFile: boolean;
  isDirectory: boolean;
  name: string;
  fullPath: string;
  file?: (ok: (f: File) => void, fail: (e: unknown) => void) => void;
  createReader?: () => {
    readEntries: (ok: (e: FsEntry[]) => void, fail: (e: unknown) => void) => void;
  };
};

async function walk(entry: FsEntry, prefix: string, out: ZipFile[]): Promise<void> {
  if (entry.isFile && entry.file) {
    const f = await new Promise<File>((ok, fail) => entry.file!(ok, fail));
    out.push({ path: prefix + entry.name, data: await readBytes(f) });
  } else if (entry.isDirectory && entry.createReader) {
    const reader = entry.createReader();
    // readEntries returns results in batches; call until it returns an empty list.
    for (;;) {
      const batch = await new Promise<FsEntry[]>((ok, fail) => reader.readEntries(ok, fail));
      if (!batch.length) break;
      for (const child of batch) await walk(child, `${prefix}${entry.name}/`, out);
    }
  }
}

/**
 * Files from a drop event's dataTransfer.items, expanding dropped folders.
 * Call it synchronously from the drop handler (entries are only valid there);
 * the returned promise may then be awaited.
 */
export function filesFromDataTransfer(items: DataTransferItemList | DataTransferItem[]): Promise<ZipFile[]> {
  const entries: FsEntry[] = [];
  const plain: File[] = [];
  for (const item of Array.from(items)) {
    if (item.kind !== "file") continue;
    const getEntry = (item as DataTransferItem & { webkitGetAsEntry?: () => FsEntry | null }).webkitGetAsEntry;
    const entry = getEntry ? getEntry.call(item) : null;
    if (entry) entries.push(entry);
    else {
      const f = item.getAsFile();
      if (f) plain.push(f);
    }
  }
  return (async () => {
    const out: ZipFile[] = [];
    for (const e of entries) await walk(e, "", out);
    for (const f of plain) out.push({ path: f.name, data: await readBytes(f) });
    return out;
  })();
}
