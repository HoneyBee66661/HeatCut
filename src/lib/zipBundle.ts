// Minimal STORE-method ZIP writer (no dependencies) used by the campaign page's
// "download the whole pack" action.
//
// WHY: browsers gate AUTOMATIC MULTIPLE DOWNLOADS. Under default settings Chrome
// lets the first file through and holds the rest behind a one-time "allow
// multiple downloads" prompt — if the user misses it, files 2..N never reach the
// disk (measured: 1/4 landed by default vs 4/4 with the policy forced to allow).
// One archive = ONE download = always saved, no permission dialog.
//
// Method 0 (stored, no compression) is deliberate: the payload is already
// mp4/m4a, so deflate would burn CPU for ~0% gain. Clients only need to read
// local headers + CRC32.

/** CRC-32 (poly 0xEDB88320) — required by the ZIP container. */
const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n += 1) {
    let c = n;
    for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c >>> 0;
  }
  return table;
})();

export function crc32(bytes: Uint8Array): number {
  let c = 0xffffffff;
  for (let i = 0; i < bytes.length; i += 1) c = CRC_TABLE[(c ^ bytes[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

/** DOS date/time pair (local clock, as the spec expects). */
function dosStamp(d: Date): { time: number; date: number } {
  const time = (d.getHours() << 11) | (d.getMinutes() << 5) | Math.floor(d.getSeconds() / 2);
  const date = ((d.getFullYear() - 1980) << 9) | ((d.getMonth() + 1) << 5) | d.getDate();
  return { time: time & 0xffff, date: date & 0xffff };
}

class Bytes {
  private parts: Uint8Array[] = [];
  private length = 0;

  u16(v: number): this {
    this.parts.push(new Uint8Array([v & 0xff, (v >>> 8) & 0xff]));
    this.length += 2;
    return this;
  }

  u32(v: number): this {
    this.parts.push(new Uint8Array([v & 0xff, (v >>> 8) & 0xff, (v >>> 16) & 0xff, (v >>> 24) & 0xff]));
    this.length += 4;
    return this;
  }

  raw(b: Uint8Array): this {
    this.parts.push(b);
    this.length += b.length;
    return this;
  }

  get size(): number {
    return this.length;
  }

  done(): Blob {
    return new Blob(this.parts as BlobPart[]);
  }
}

export interface ZipEntry {
  name: string;
  /** Blob payload — kept as a Blob part so the browser, not the JS heap, owns it. */
  blob: Blob;
  crc: number;
  size: number;
}

/**
 * Read a blob once to obtain its CRC32 + size (the ZIP index needs both).
 * Returns the entry with the ORIGINAL blob, so only this transient ArrayBuffer
 * is ever held per file.
 */
export async function zipEntry(name: string, blob: Blob): Promise<ZipEntry> {
  const buf = new Uint8Array(await blob.arrayBuffer());
  return { name, blob, crc: crc32(buf), size: buf.length };
}

/** STORE-only ZIP container from already-checksummed entries. */
export function buildZip(entries: ZipEntry[]): Blob {
  const enc = new TextEncoder();
  const stamp = dosStamp(new Date());
  const parts: (Uint8Array | Blob)[] = [];
  const central = new Bytes();
  let offset = 0;

  entries.forEach((e) => {
    const name = enc.encode(e.name);
    const head = new Bytes()
      .u32(0x04034b50) // local file header
      .u16(20) // version needed
      .u16(0x0800) // UTF-8 names
      .u16(0) // method 0 = stored
      .u16(stamp.time)
      .u16(stamp.date)
      .u32(e.crc)
      .u32(e.size) // compressed == uncompressed
      .u32(e.size)
      .u16(name.length)
      .u16(0) // extra length
      .raw(name)
      .done();
    parts.push(head, e.blob);

    const localSize = head.size + e.size;
    central
      .u32(0x02014b50) // central directory header
      .u16(20)
      .u16(20)
      .u16(0x0800)
      .u16(0)
      .u16(stamp.time)
      .u16(stamp.date)
      .u32(e.crc)
      .u32(e.size)
      .u32(e.size)
      .u16(name.length)
      .u16(0) // extra
      .u16(0) // comment
      .u16(0) // disk number
      .u16(0) // internal attrs
      .u32(0) // external attrs
      .u32(offset)
      .raw(name);
    offset += localSize;
  });

  const end = new Bytes()
    .u32(0x06054b50) // end of central directory
    .u16(0)
    .u16(0)
    .u16(entries.length)
    .u16(entries.length)
    .u32(central.size)
    .u32(offset)
    .u16(0)
    .done();

  return new Blob([...parts, central.done(), end] as BlobPart[], { type: 'application/zip' });
}
