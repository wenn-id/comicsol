use std::collections::BTreeSet;
use std::fs::File;
use std::io::{Read, Seek, SeekFrom, Write};

use flate2::bufread::DeflateDecoder;

use crate::error::{Error, Result};
use crate::finalize::native::{crc, zip_deflate};

pub const MAX_MEMBERS: usize = 4096;
pub const MAX_MEMBER: u64 = 64 * 1024 * 1024;
pub const MAX_TOTAL: u64 = 1024 * 1024 * 1024;
const END: &[u8; 4] = b"PK\x05\x06";
const DIRECTORY: &[u8; 4] = b"PK\x01\x02";
const HEADER: &[u8; 4] = b"PK\x03\x04";
const UTF8: u16 = 1 << 11;
const DATA_DESCRIPTOR: u16 = 1 << 3;
const DEFLATE: u16 = 8;
const DATE: u16 = 33;

pub struct Member {
    pub name: String,
    pub size: u32,
    compressed_size: u32,
    offset: u64,
    crc: u32,
    flags: u16,
    directory_start: u64,
}

pub fn index(source: &mut File, name_check: impl Fn(&str) -> Result<()>) -> Result<Vec<Member>> {
    let len = source.metadata().map_err(|_| corrupt())?.len();
    let tail_len = len.min(22 + 65_535);
    if tail_len < 22 {
        return Err(corrupt());
    }
    source
        .seek(SeekFrom::Start(len - tail_len))
        .map_err(|_| corrupt())?;
    let mut tail = vec![0; tail_len as usize];
    source.read_exact(&mut tail).map_err(|_| corrupt())?;
    let (index, end) = tail
        .windows(22)
        .enumerate()
        .rev()
        .find(|(index, candidate)| {
            &candidate[0..4] == END && *index + 22 + word(candidate, 20) as usize == tail.len()
        })
        .ok_or_else(corrupt)?;
    let count = word(end, 10) as usize;
    if word(end, 4) != 0 || word(end, 6) != 0 || word(end, 8) as usize != count {
        return Err(Error::archive(
            "archive multi-disk central directory is unsupported",
        ));
    }
    let size = dword(end, 12) as u64;
    let offset = dword(end, 16) as u64;
    if count == 0xffff || size == 0xffffffff || offset == 0xffffffff {
        return Err(Error::archive(
            "archive ZIP64 central directory is unsupported",
        ));
    }
    if count > MAX_MEMBERS {
        return Err(Error::archive("archive member count exceeds the limit"));
    }
    if size > MAX_MEMBER {
        return Err(Error::archive(
            "archive central directory exceeds the metadata limit",
        ));
    }
    let central_end = len - tail_len + index as u64;
    let directory_start = central_end
        .checked_sub(size)
        .filter(|&start| offset <= start)
        .ok_or_else(corrupt)?;
    let base = directory_start - offset;
    let mut cursor = directory_start;
    source
        .seek(SeekFrom::Start(cursor))
        .map_err(|_| corrupt())?;
    let mut members = Vec::new();
    let mut folded = BTreeSet::new();
    let mut total = 0;
    let mut total_compressed = 0;
    while cursor < central_end {
        if members.len() >= MAX_MEMBERS {
            return Err(Error::archive("archive member count exceeds the limit"));
        }
        let mut fixed = [0; 46];
        source.read_exact(&mut fixed).map_err(|_| corrupt())?;
        if &fixed[..4] != DIRECTORY {
            return Err(corrupt());
        }
        let compressed_size = dword(&fixed, 20);
        let size = dword(&fixed, 24);
        let local_offset = dword(&fixed, 42);
        let member_disk = word(&fixed, 34);
        if [compressed_size, size, local_offset].contains(&u32::MAX) || member_disk == u16::MAX {
            return Err(Error::archive(
                "archive ZIP64 central directory is unsupported",
            ));
        }
        if member_disk != 0 {
            return Err(Error::archive(
                "archive multi-disk central directory is unsupported",
            ));
        }
        let flags = word(&fixed, 8);
        if flags & 1 != 0 {
            return Err(Error::archive("archive contains an encrypted member"));
        }
        if flags & !(UTF8 | DATA_DESCRIPTOR | 6) != 0 || word(&fixed, 10) != DEFLATE {
            return Err(Error::archive("archive member compression is unsupported"));
        }
        let attributes = dword(&fixed, 38);
        match fixed[5] {
            3 if (attributes >> 16) & 0xf000 == 0xa000 => {
                return Err(Error::archive("archive contains a symlink member"));
            }
            3 if (attributes >> 16) & 0xf000 == 0x8000 => {}
            0 if attributes & 0x0400 != 0 => {
                return Err(Error::archive("archive contains a Windows reparse member"));
            }
            0 if attributes & 0x10 == 0 => {}
            0 | 3 => return Err(Error::archive("archive contains a non-regular member")),
            _ => return Err(Error::archive("archive member platform is unsupported")),
        }
        total += u64::from(size);
        total_compressed += u64::from(compressed_size);
        check_limits(size, compressed_size, total, total_compressed)?;
        let name_len = word(&fixed, 28) as usize;
        let skip = u64::from(word(&fixed, 30)) + u64::from(word(&fixed, 32));
        cursor += 46 + name_len as u64 + skip;
        if cursor > central_end {
            return Err(corrupt());
        }
        let mut name = vec![0; name_len];
        source.read_exact(&mut name).map_err(|_| corrupt())?;
        source
            .seek(SeekFrom::Start(cursor))
            .map_err(|_| corrupt())?;
        let name = if flags & UTF8 != 0 {
            String::from_utf8(name).map_err(|_| corrupt())?
        } else {
            cp437(&name)
        };
        name_check(&name)?;
        if !folded.insert(caseless::default_case_fold_str(&name)) {
            return Err(Error::archive("archive member names have a case collision"));
        }
        let offset = base + u64::from(local_offset);
        if offset >= directory_start {
            return Err(corrupt());
        }
        members.push(Member {
            name,
            size,
            compressed_size,
            offset,
            crc: dword(&fixed, 16),
            flags,
            directory_start,
        });
    }
    if members.len() != count {
        return Err(Error::archive(
            "archive central directory member count is inconsistent",
        ));
    }
    if members.windows(2).any(|pair| pair[0].name >= pair[1].name) {
        return Err(Error::archive(
            "archive members are not in canonical sorted order",
        ));
    }
    let mut positions: Vec<_> = members.iter_mut().collect();
    positions.sort_unstable_by_key(|member| member.offset);
    for index in 1..positions.len() {
        positions[index - 1].directory_start = positions[index].offset;
    }
    Ok(members)
}

pub fn read(source: &mut File, member: &Member, limit: u64) -> Result<Vec<u8>> {
    let size = u64::from(member.size);
    if size > limit {
        return Err(Error::archive(
            "archive member exceeded its declared size while reading",
        ));
    }
    source
        .seek(SeekFrom::Start(member.offset))
        .map_err(|_| corrupt())?;
    let mut fixed = [0; 30];
    source.read_exact(&mut fixed).map_err(|_| corrupt())?;
    if &fixed[..4] != HEADER || word(&fixed, 6) != member.flags || word(&fixed, 8) != DEFLATE {
        return Err(corrupt());
    }
    if member.flags & DATA_DESCRIPTOR == 0
        && (dword(&fixed, 14), dword(&fixed, 18), dword(&fixed, 22))
            != (member.crc, member.compressed_size, member.size)
    {
        return Err(corrupt());
    }
    let mut name = vec![0; word(&fixed, 26) as usize];
    let data_start = member.offset + 30 + name.len() as u64 + u64::from(word(&fixed, 28));
    let compressed = u64::from(member.compressed_size);
    if data_start + compressed > member.directory_start {
        return Err(corrupt());
    }
    source.read_exact(&mut name).map_err(|_| corrupt())?;
    let local = if member.flags & UTF8 != 0 {
        String::from_utf8(name).map_err(|_| corrupt())?
    } else {
        cp437(&name)
    };
    if local != member.name {
        return Err(corrupt());
    }
    source
        .seek(SeekFrom::Start(data_start))
        .map_err(|_| corrupt())?;
    let mut bytes = Vec::with_capacity(member.size as usize);
    let input = std::io::BufReader::new(source.take(compressed));
    let mut decoded = DeflateDecoder::new(input);
    (&mut decoded)
        .take(size + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| corrupt())?;
    if decoded.total_in() != compressed || bytes.len() as u64 != size || crc(&bytes) != member.crc {
        return Err(corrupt());
    }
    Ok(bytes)
}

pub struct Writer<'a> {
    file: &'a mut File,
    central: Vec<u8>,
    count: u16,
    total: u64,
    total_compressed: u64,
}

impl<'a> Writer<'a> {
    pub fn new(file: &'a mut File) -> Self {
        Self {
            file,
            central: Vec::new(),
            count: 0,
            total: 0,
            total_compressed: 0,
        }
    }

    pub fn add(&mut self, name: &str, bytes: &[u8]) -> Result<()> {
        if self.count as usize >= MAX_MEMBERS || name.len() > u16::MAX as usize {
            return Err(Error::archive("archive member count exceeds the limit"));
        }
        let offset = self.file.stream_position().map_err(|_| corrupt())?;
        let compressed = zip_deflate(bytes).ok_or_else(corrupt)?;
        self.total += bytes.len() as u64;
        self.total_compressed += compressed.len() as u64;
        check_limits(
            bytes.len() as u32,
            compressed.len() as u32,
            self.total,
            self.total_compressed,
        )?;
        let mut common = Vec::new();
        for value in [20, if name.is_ascii() { 0 } else { UTF8 }, DEFLATE, 0, DATE] {
            common.extend(value.to_le_bytes());
        }
        for value in [crc(bytes), compressed.len() as u32, bytes.len() as u32] {
            common.extend(value.to_le_bytes());
        }
        common.extend((name.len() as u16).to_le_bytes());
        common.extend(0u16.to_le_bytes());
        self.file.write_all(HEADER).map_err(|_| corrupt())?;
        self.file.write_all(&common).map_err(|_| corrupt())?;
        self.file
            .write_all(name.as_bytes())
            .map_err(|_| corrupt())?;
        self.file.write_all(&compressed).map_err(|_| corrupt())?;
        self.central.extend(DIRECTORY);
        self.central.extend([20, 3]);
        self.central.extend(&common);
        self.central.extend([0; 6]);
        self.central.extend((0o100644u32 << 16).to_le_bytes());
        self.central.extend((offset as u32).to_le_bytes());
        self.central.extend(name.as_bytes());
        self.count += 1;
        Ok(())
    }

    pub fn finish(self) -> Result<()> {
        let offset = self.file.stream_position().map_err(|_| corrupt())? as u32;
        self.file.write_all(&self.central).map_err(|_| corrupt())?;
        let mut end = END.to_vec();
        for value in [0, 0, self.count, self.count] {
            end.extend(value.to_le_bytes());
        }
        end.extend((self.central.len() as u32).to_le_bytes());
        end.extend(offset.to_le_bytes());
        end.extend([0; 2]);
        self.file.write_all(&end).map_err(|_| corrupt())
    }
}

fn check_limits(size: u32, compressed: u32, total: u64, total_compressed: u64) -> Result<()> {
    if u64::from(size) > MAX_MEMBER {
        return Err(Error::archive("archive member exceeds the member limit"));
    }
    if total > MAX_TOTAL {
        return Err(Error::archive(
            "archive aggregate uncompressed size exceeds the limit",
        ));
    }
    if compressed == 0 {
        return Err(Error::archive(
            "archive member has an invalid compressed size",
        ));
    }
    if u64::from(compressed) > MAX_MEMBER {
        return Err(Error::archive(
            "archive member exceeds the compressed-byte limit",
        ));
    }
    if total_compressed > MAX_TOTAL {
        return Err(Error::archive(
            "archive aggregate compressed size exceeds the limit",
        ));
    }
    if u64::from(size) > u64::from(compressed) * 200 {
        return Err(Error::archive(
            "archive member exceeds the compression ratio limit",
        ));
    }
    Ok(())
}

fn word(bytes: &[u8], at: usize) -> u16 {
    u16::from_le_bytes(bytes[at..at + 2].try_into().expect("ZIP field"))
}

fn dword(bytes: &[u8], at: usize) -> u32 {
    u32::from_le_bytes(bytes[at..at + 4].try_into().expect("ZIP field"))
}

fn corrupt() -> Error {
    Error::archive("archive ZIP or central directory is corrupt")
}

fn cp437(bytes: &[u8]) -> String {
    const HIGH: [char; 128] = [
        'Ç', 'ü', 'é', 'â', 'ä', 'à', 'å', 'ç', 'ê', 'ë', 'è', 'ï', 'î', 'ì', 'Ä', 'Å', 'É', 'æ',
        'Æ', 'ô', 'ö', 'ò', 'û', 'ù', 'ÿ', 'Ö', 'Ü', '¢', '£', '¥', '₧', 'ƒ', 'á', 'í', 'ó', 'ú',
        'ñ', 'Ñ', 'ª', 'º', '¿', '⌐', '¬', '½', '¼', '¡', '«', '»', '░', '▒', '▓', '│', '┤', '╡',
        '╢', '╖', '╕', '╣', '║', '╗', '╝', '╜', '╛', '┐', '└', '┴', '┬', '├', '─', '┼', '╞', '╟',
        '╚', '╔', '╩', '╦', '╠', '═', '╬', '╧', '╨', '╤', '╥', '╙', '╘', '╒', '╓', '╫', '╪', '┘',
        '┌', '█', '▄', '▌', '▐', '▀', 'α', 'ß', 'Γ', 'π', 'Σ', 'σ', 'µ', 'τ', 'Φ', 'Θ', 'Ω', 'δ',
        '∞', 'φ', 'ε', '∩', '≡', '±', '≥', '≤', '⌠', '⌡', '÷', '≈', '°', '∙', '·', '√', 'ⁿ', '²',
        '■', '\u{a0}',
    ];
    bytes
        .iter()
        .map(|&byte| {
            if byte < 128 {
                char::from(byte)
            } else {
                HIGH[usize::from(byte - 128)]
            }
        })
        .collect()
}
