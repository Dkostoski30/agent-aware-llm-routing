/** A file of the run request, as schemas/run-request.v1.json defines it. The limits on files (duplicate ids,
 * agent.max_files, agent.max_file_bytes) stay with the agent, which says why it refuses a run. */
export interface RunFile {
  id: string;
  name: string;
  type: 'csv' | 'text';
  content: string;
}

export interface RefusedFile {
  refused: string;
}

const TYPES = new Map<string, RunFile['type']>([
  ['csv', 'csv'],
  ['txt', 'text'],
  ['md', 'text'],
]);

/** What the file picker offers, in the form its accept attribute takes. */
export const ACCEPTED_FILES = [...TYPES.keys()].map((extension) => `.${extension}`).join(',');

// maxLength of a file's id in schemas/run-request.v1.json.
const MAX_ID_LENGTH = 64;

// A letter, digit or combining mark. Left after taking out a-z and 0-9, it is one the id could not keep.
const LOST_IN_ID = /[\p{L}\p{N}\p{M}]/u;

/** The id and type a file's name gives it, or why it cannot go in a run request. The model names a file by its id
 * (D13). An id keeps only a-z and 0-9, so a name it would lose letters from is refused rather than sent under an id the
 * user never gave: продажби-2025.csv would be 2025, and приходи-2025.csv too. Punctuation only becomes dashes. */
export function fileFields(name: string): Pick<RunFile, 'id' | 'type'> | RefusedFile {
  const dot = name.lastIndexOf('.');
  const type = dot < 0 ? undefined : TYPES.get(name.slice(dot + 1).toLowerCase());
  if (type === undefined) return { refused: `${name}: only ${ACCEPTED_FILES} can be attached` };
  const stem = name.slice(0, dot);
  const id = stem
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-/, '')
    .slice(0, MAX_ID_LENGTH)
    .replace(/-$/, '');
  const unusable = id === '' || LOST_IN_ID.test(stem.replace(/[a-zA-Z0-9]/g, ''));
  return unusable ? { refused: `${name}: rename it with Latin letters and digits` } : { id, type };
}

/** The file as a run request carries it, or why it cannot go in one. fatal: a CSV saved in another encoding is
 * refused here, since File.text() would turn its letters into replacement characters without a word. */
export async function readRunFile(file: File): Promise<RunFile | RefusedFile> {
  const fields = fileFields(file.name);
  if ('refused' in fields) return fields;
  try {
    const bytes = await file.arrayBuffer();
    return {
      ...fields,
      name: file.name,
      content: new TextDecoder('utf-8', { fatal: true }).decode(bytes),
    };
  } catch (error) {
    // TextDecoder throws a TypeError on bytes that are not UTF-8, arrayBuffer() a DOMException on a file it cannot read
    // and a RangeError on one it has no memory for.
    if (error instanceof TypeError) return { refused: `${file.name}: the content is not UTF-8` };
    if (error instanceof RangeError) return { refused: `${file.name}: too large to read` };
    if (error instanceof DOMException) return { refused: `${file.name}: could not be read` };
    throw error;
  }
}
