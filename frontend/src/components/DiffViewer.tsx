import { useMemo } from 'react';

interface DiffRecord {
  file: string;
  original: string;
  modified: string;
  agentId?: string;
  stage?: number;
  timestamp?: number;
}

interface Props {
  diff: DiffRecord;
  onClose: () => void;
}

interface DiffLine {
  type: 'add' | 'del' | 'ctx' | 'hunk';
  oldNum?: number;
  newNum?: number;
  content: string;
}

function computeUnifiedDiff(original: string, modified: string): DiffLine[] {
  const oldLines = original.split('\n');
  const newLines = modified.split('\n');

  const n = oldLines.length;
  const m = newLines.length;
  const max = n + m;
  const v = new Int32Array(2 * max + 2);
  const trace: Int32Array[] = [];

  for (let d = 0; d <= max; d++) {
    const tv = new Int32Array(v);
    trace.push(tv);
    for (let k = -d; k <= d; k += 2) {
      let x: number;
      if (k === -d || (k !== d && v[k - 1 + max] < v[k + 1 + max])) {
        x = v[k + 1 + max];
      } else {
        x = v[k - 1 + max] + 1;
      }
      let y = x - k;
      while (x < n && y < m && oldLines[x] === newLines[y]) { x++; y++; }
      v[k + max] = x;
      if (x >= n && y >= m) {
        const edits: Array<{ type: 'keep' | 'insert' | 'delete'; oldIdx?: number; newIdx?: number }> = [];
        let cx = n, cy = m;
        for (let dd = trace.length - 1; dd > 0; dd--) {
          const prev = trace[dd - 1];
          const kk = cx - cy;
          let prevX: number;
          if (kk === -dd || (kk !== dd && prev[kk - 1 + max] < prev[kk + 1 + max])) {
            prevX = prev[kk + 1 + max];
          } else {
            prevX = prev[kk - 1 + max] + 1;
          }
          const py2 = prevX - (kk === -dd || (kk !== dd && prev[kk - 1 + max] < prev[kk + 1 + max]) ? kk + 1 : kk - 1);

          while (cx > prevX && cy > py2) {
            cx--; cy--;
            edits.push({ type: 'keep', oldIdx: cx, newIdx: cy });
          }
          if (dd > 0) {
            if (cx === prevX && cy > py2) {
              cy--;
              edits.push({ type: 'insert', newIdx: cy });
            } else if (cx > prevX) {
              cx--;
              edits.push({ type: 'delete', oldIdx: cx });
            }
          }
        }
        while (cx > 0 && cy > 0) {
          cx--; cy--;
          edits.push({ type: 'keep', oldIdx: cx, newIdx: cy });
        }
        edits.reverse();

        const result: DiffLine[] = [];
        let oNum = 0, nNum = 0;
        for (const e of edits) {
          if (e.type === 'keep') {
            oNum++; nNum++;
            result.push({ type: 'ctx', oldNum: oNum, newNum: nNum, content: oldLines[e.oldIdx!] });
          } else if (e.type === 'delete') {
            oNum++;
            result.push({ type: 'del', oldNum: oNum, content: oldLines[e.oldIdx!] });
          } else {
            nNum++;
            result.push({ type: 'add', newNum: nNum, content: newLines[e.newIdx!] });
          }
        }
        return result;
      }
    }
  }

  // Fallback: show all as delete + add
  const result: DiffLine[] = [];
  oldLines.forEach((l, i) => result.push({ type: 'del', oldNum: i + 1, content: l }));
  newLines.forEach((l, i) => result.push({ type: 'add', newNum: i + 1, content: l }));
  return result;
}

export default function DiffViewer({ diff, onClose }: Props) {
  const lines = useMemo(() => {
    if (!diff.original && !diff.modified) return [];
    return computeUnifiedDiff(diff.original || '', diff.modified || '');
  }, [diff.original, diff.modified]);

  const addCount = lines.filter((l) => l.type === 'add').length;
  const delCount = lines.filter((l) => l.type === 'del').length;

  return (
    <div className="diff-overlay" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="diff-dialog">
        <div className="diff-header">
          <div className="diff-header-title">文件对比</div>
          <div className="diff-header-file">{diff.file}</div>
          <span style={{ color: '#3fb950', fontSize: 12, marginLeft: 8 }}>+{addCount}</span>
          <span style={{ color: '#f85149', fontSize: 12, marginLeft: 4 }}>-{delCount}</span>
          {diff.timestamp && (
            <span style={{ color: '#8b949e', fontSize: 11, marginLeft: 12 }}>
              {new Date(diff.timestamp).toLocaleString('zh-CN')}
            </span>
          )}
          <button className="diff-close" onClick={onClose}>✕</button>
        </div>
        <div className="diff-body">
          {lines.map((line, i) => (
            <div key={i} className={`diff-line diff-line-${line.type}`}>
              <span className="diff-line-num">{line.oldNum ?? ''}</span>
              <span className="diff-line-num">{line.newNum ?? ''}</span>
              <span className="diff-line-content">
                {line.type === 'add' ? '+' : line.type === 'del' ? '-' : ' '} {line.content}
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
