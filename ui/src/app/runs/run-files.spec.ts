import runRequestSchema from '../../../../schemas/run-request.v1.json';
import { ACCEPTED_FILES, RunFile, fileFields, readRunFile } from './run-files';

const FILE_SCHEMA = runRequestSchema.properties.files.items.properties;

describe('fileFields', () => {
  it.each([
    ['sales.csv', 'sales', 'csv'],
    ['SALES.CSV', 'sales', 'csv'],
    ['Sales Q3 2025.csv', 'sales-q3-2025', 'csv'],
    ['sales_2025.v2.csv', 'sales-2025-v2', 'csv'],
    ['__sales__.csv', 'sales', 'csv'],
    ['contract-2025.txt', 'contract-2025', 'text'],
    ['policies.md', 'policies', 'text'],
    ['Policies.MD', 'policies', 'text'],
  ])('reads %s as id %s, type %s', (name, id, type) => {
    expect(fileFields(name)).toEqual({ id, type });
  });

  it('cuts the id at the schema length and leaves no dash at the cut', () => {
    const max = FILE_SCHEMA.id.maxLength;
    expect(fileFields(`${'a'.repeat(max + 6)}.csv`)).toEqual({ id: 'a'.repeat(max), type: 'csv' });
    expect(fileFields(`${'a'.repeat(max - 1)}-b.csv`)).toEqual({
      id: 'a'.repeat(max - 1),
      type: 'csv',
    });
  });

  it.each([['report.pdf'], ['data'], ['archive.csv.zip']])(
    'refuses %s for its extension, naming the file',
    (name) => {
      const fields = fileFields(name);
      expect(fields).toEqual({ refused: expect.stringContaining(name) });
      expect(fields).toEqual({ refused: expect.stringContaining(ACCEPTED_FILES) });
    },
  );

  it.each([
    ['продажби.csv'],
    ['продажби-2025.csv'],
    ['Договор_A.txt'],
    ['čeština.csv'],
    ['c\u030Ceska.csv'],
    ['İstanbul.csv'],
    ['٢٠٢٥.csv'],
    ['___.txt'],
    ['.csv'],
  ])('refuses %s instead of building a surprising id from what is left', (name) => {
    expect(fileFields(name)).toEqual({ refused: expect.stringContaining(name) });
    expect(fileFields(name)).toEqual({ refused: expect.stringContaining('Latin letters') });
  });

  it('takes punctuation and spaces, which the id turns into dashes', () => {
    expect(fileFields('a (final), v2!.csv')).toEqual({ id: 'a-final-v2', type: 'csv' });
  });

  // Contract test (D12): what the UI builds is what schemas/run-request.v1.json accepts.
  it('builds only ids and types that schemas/run-request.v1.json accepts', () => {
    const names = ['sales.csv', 'Sales Q3 2025.csv', `${'a-'.repeat(50)}.md`, 'x.txt'];
    for (const name of names) {
      const fields = fileFields(name) as Pick<RunFile, 'id' | 'type'>;
      expect(fields.id).toMatch(new RegExp(FILE_SCHEMA.id.pattern));
      expect(fields.id.length).toBeLessThanOrEqual(FILE_SCHEMA.id.maxLength);
      expect(FILE_SCHEMA.type.enum).toContain(fields.type);
    }
  });
});

describe('readRunFile', () => {
  it('returns the fields a run request needs, with the file name as attached', async () => {
    const file = await readRunFile(new File(['region,revenue\nNorth,1\n'], 'Sales.csv'));
    expect(file).toEqual({
      id: 'sales',
      name: 'Sales.csv',
      type: 'csv',
      content: 'region,revenue\nNorth,1\n',
    });
    const keys = Object.keys(FILE_SCHEMA);
    for (const key of Object.keys(file)) expect(keys).toContain(key);
    for (const key of runRequestSchema.properties.files.items.required) {
      expect(Object.keys(file)).toContain(key);
    }
  });

  it('keeps non-Latin text and drops the byte order mark Excel writes', async () => {
    const bytes = new TextEncoder().encode('﻿регион\nСевер\n');
    const file = await readRunFile(new File([bytes], 'sales.csv'));
    expect(file).toMatchObject({ content: 'регион\nСевер\n' });
  });

  it('refuses content that is not UTF-8 instead of garbling it', async () => {
    // "регион" in windows-1251, the encoding a Cyrillic CSV from Excel may have.
    const bytes = new Uint8Array([0xf0, 0xe5, 0xe3, 0xe8, 0xee, 0xed]);
    expect(await readRunFile(new File([bytes], 'sales.csv'))).toEqual({
      refused: expect.stringContaining('sales.csv'),
    });
    expect(await readRunFile(new File([bytes], 'sales.csv'))).toEqual({
      refused: expect.stringContaining('UTF-8'),
    });
  });

  it('says so when the browser cannot read the file', async () => {
    const file = new File(['a'], 'sales.csv');
    file.arrayBuffer = () => Promise.reject(new DOMException('gone', 'NotReadableError'));
    expect(await readRunFile(file)).toEqual({ refused: expect.stringContaining('read') });
  });

  it('says a file is too large when the browser cannot hold it', async () => {
    const file = new File(['a'], 'sales.csv');
    file.arrayBuffer = () => Promise.reject(new RangeError('Array buffer allocation failed'));
    const refused = await readRunFile(file);
    expect(refused).toEqual({ refused: expect.stringContaining('sales.csv') });
    expect(refused).toEqual({ refused: expect.stringContaining('too large') });
  });

  it('does not read a file it refuses for its name', async () => {
    const file = new File(['a'], 'report.pdf');
    file.arrayBuffer = () => Promise.reject(new Error('read a refused file'));
    expect(await readRunFile(file)).toEqual({ refused: expect.stringContaining('report.pdf') });
  });
});
