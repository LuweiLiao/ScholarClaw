import { useCallback, useRef, useEffect } from 'react';

interface Props {
  storageKey: string;
  defaultWidth: number;
  minWidth: number;
  maxWidth: number;
  side: 'left' | 'right';
  onResize: (width: number) => void;
}

export default function ResizeHandle({ storageKey, defaultWidth, minWidth, maxWidth, side, onResize }: Props) {
  const dragging = useRef(false);
  const startX = useRef(0);
  const startW = useRef(defaultWidth);

  useEffect(() => {
    const saved = localStorage.getItem(`scholar-panel-${storageKey}`);
    if (saved) {
      const w = Math.max(minWidth, Math.min(maxWidth, parseInt(saved, 10)));
      if (!isNaN(w)) onResize(w);
    }
  }, [storageKey, minWidth, maxWidth, onResize]);

  const onMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    dragging.current = true;
    startX.current = e.clientX;
    const el = side === 'left' ? e.currentTarget.previousElementSibling : e.currentTarget.nextElementSibling;
    startW.current = el ? el.getBoundingClientRect().width : defaultWidth;
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';

    const onMove = (ev: MouseEvent) => {
      if (!dragging.current) return;
      const delta = ev.clientX - startX.current;
      const newW = side === 'left'
        ? Math.max(minWidth, Math.min(maxWidth, startW.current + delta))
        : Math.max(minWidth, Math.min(maxWidth, startW.current - delta));
      onResize(newW);
    };

    const onUp = () => {
      dragging.current = false;
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      const el2 = side === 'left' ? e.currentTarget.previousElementSibling : e.currentTarget.nextElementSibling;
      if (el2) {
        localStorage.setItem(`scholar-panel-${storageKey}`, String(Math.round(el2.getBoundingClientRect().width)));
      }
    };

    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  }, [side, defaultWidth, minWidth, maxWidth, onResize, storageKey]);

  return (
    <div
      className="resize-handle"
      onMouseDown={onMouseDown}
    />
  );
}
