// Run from repository root using the bundled artifact-tool runtime.
import fs from 'node:fs/promises';
import path from 'node:path';
import { Workbook, SpreadsheetFile } from '@oai/artifact-tool';

const root = process.cwd();
const data = JSON.parse(await fs.readFile(path.join(root, 'tmp/rr022-review-data-20260930-v1.json'), 'utf8'));
const wb = Workbook.create();
const outDir = path.join(root, 'outputs/01a0063b-1c78-7a01-9707-bc36b9e5ba2e');
await fs.mkdir(outDir, {recursive: true});
function textSafe(value) {
  return typeof value === 'string' && (/^[=+@-]/.test(value) || /^\d{4}-\d{2}-\d{2}T/.test(value)) ? "'" + value : value;
}
for (const [name, rows] of Object.entries(data.sheets)) {
  const sheet = wb.worksheets.add(name);
  const width = rows[0].length;
  const range = sheet.getRangeByIndexes(0, 0, rows.length, width);
  range.setNumberFormat('@');
  range.values = rows.map(row => row.map(textSafe));
  range.format.font = {name: 'Microsoft YaHei', size: 10};
  range.format.rowHeight = 24;
  range.format.verticalAlignment = 'center';
  sheet.showGridLines = false;
  sheet.freezePanes.freezeRows(1);
  const header = sheet.getRangeByIndexes(0, 0, 1, width);
  header.format.fill = '#203C4C';
  header.format.font = {name: 'Microsoft YaHei', size: 10, bold: true, color: '#FFFFFF'};
  header.format.rowHeight = 42;
  header.format.wrapText = true;
  if (name === '填写说明') {
    sheet.getRange('A:A').format.columnWidth = 17;
    sheet.getRange('B:B').format.columnWidth = 108;
    range.format.wrapText = true;
    range.format.rowHeight = 76;
    header.format.rowHeight = 30;
  } else if (name === '复核填写') {
    sheet.getRange('A:B').format.columnWidth = 12;
    sheet.getRange('C:G').format.columnWidth = 19;
    sheet.getRange('H:I').format.columnWidth = 30;
    sheet.getRange('J:K').format.columnWidth = 17;
    range.format.wrapText = true;
    range.format.rowHeight = 54;
    range.format.borders = {preset: 'all', style:'thin', color:'#CFD6DC'};
    sheet.getRange('C2:K13').format.fill = '#FFF8E7';
    for (const col of ['C','D','F']) {
      sheet.getRange(`${col}2:${col}13`).dataValidation = {rule: {type:'list', values:['是','否','信息不足']}};
    }
    sheet.getRange('E2:E13').dataValidation = {rule: {type:'list', values:['是','否','前序应终止','信息不足']}};
    sheet.getRange('G2:G13').dataValidation = {rule: {type:'list', values:['是','否','不适用','信息不足']}};
  } else if (name === 'PR原始树') {
    sheet.getRange('A:A').format.columnWidth = 12;
    sheet.getRange('B:B').format.columnWidth = 100;
    for (const col of ['C','D','F','G']) sheet.getRange(`${col}:${col}`).format.columnWidth = 14;
    for (const col of ['E','H']) sheet.getRange(`${col}:${col}`).format.columnWidth = 48;
  } else {
    sheet.getRange('A:A').format.columnWidth = 13;
    sheet.getRange('B:B').format.columnWidth = 28;
    sheet.getRange('C:C').format.columnWidth = 105;
    range.format.wrapText = true;
    for (let i=1; i<rows.length; i++) {
      const length = String(rows[i][2] ?? '').length;
      sheet.getRangeByIndexes(i, 0, 1, width).format.rowHeight = Math.min(350, Math.max(28, Math.ceil(length/85)*19));
    }
  }
}
console.log((await wb.inspect({kind:'region',sheetId:'复核填写',range:'A1:K5',maxChars:2000,tableMaxRows:5,tableMaxCols:11})).ndjson);
console.log((await wb.inspect({kind:'match', searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!',options:{useRegex:true,maxResults:20},maxChars:1200})).ndjson);
for (const [name, range, filename] of [['复核填写','A1:G7','review-preview.png'],['授权事实','A1:C15','review-policy-preview.png'],['来源材料','A1:C20','review-source-preview.png']]) {
  const preview = await wb.render({sheetName:name,range,scale:1,format:'png'});
  await fs.writeFile(path.join(root,'tmp',filename), new Uint8Array(await preview.arrayBuffer()));
}
const output = await SpreadsheetFile.exportXlsx(wb);
const dest = path.join(outDir, 'E2AG-rr022-independent-review-v0.4-20260930.xlsx');
await output.save(dest);
console.log(JSON.stringify({output:dest}));
