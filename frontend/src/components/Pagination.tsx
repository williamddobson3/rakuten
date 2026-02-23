import { useState } from 'react';
import {
  ChevronLeft,
  ChevronRight,
  ChevronsLeft,
  ChevronsRight,
} from 'lucide-react';
import { cn } from '../lib/utils';

interface Props {
  page: number;
  totalPages: number;
  onPageChange: (page: number) => void;
}

export default function Pagination({ page, totalPages, onPageChange }: Props) {
  const [jumpInput, setJumpInput] = useState('');

  if (totalPages <= 1) return null;

  /* ── Build visible page numbers ─────────────────────── */
  const getPages = (): (number | 'left-dot' | 'right-dot')[] => {
    const pages: (number | 'left-dot' | 'right-dot')[] = [];

    // For small total pages, show all
    if (totalPages <= 9) {
      for (let i = 1; i <= totalPages; i++) pages.push(i);
      return pages;
    }

    // Always show first page
    pages.push(1);

    // Left ellipsis or page 2
    if (page > 4) {
      pages.push('left-dot');
    } else {
      for (let i = 2; i < Math.max(2, page - 1); i++) pages.push(i);
    }

    // Pages around current (current-1, current, current+1)
    for (
      let i = Math.max(2, page - 1);
      i <= Math.min(totalPages - 1, page + 1);
      i++
    ) {
      if (!pages.includes(i)) pages.push(i);
    }

    // Right ellipsis or remaining pages
    if (page < totalPages - 3) {
      pages.push('right-dot');
    } else {
      for (
        let i = Math.min(totalPages - 1, page + 2);
        i < totalPages;
        i++
      ) {
        if (!pages.includes(i)) pages.push(i);
      }
    }

    // Always show last page
    if (!pages.includes(totalPages)) pages.push(totalPages);

    return pages;
  };

  const handleJump = () => {
    const target = parseInt(jumpInput, 10);
    if (!isNaN(target) && target >= 1 && target <= totalPages) {
      onPageChange(target);
      setJumpInput('');
    }
  };

  return (
    <div className="flex flex-col items-center gap-3 mt-6">
      {/* ── Main pagination row ──────────── */}
      <div className="flex items-center gap-1">
        {/* First page */}
        <button
          disabled={page <= 1}
          onClick={() => onPageChange(1)}
          title="最初のページ"
          className={navBtnClass}
        >
          <ChevronsLeft className="w-4 h-4" />
        </button>

        {/* Previous */}
        <button
          disabled={page <= 1}
          onClick={() => onPageChange(page - 1)}
          title="前のページ"
          className={navBtnClass}
        >
          <ChevronLeft className="w-4 h-4" />
          <span className="hidden sm:inline">前へ</span>
        </button>

        {/* Page numbers */}
        <div className="flex items-center gap-1 mx-1">
          {getPages().map((p, i) =>
            p === 'left-dot' || p === 'right-dot' ? (
              <span
                key={p + '-' + i}
                className="w-9 h-9 flex items-center justify-center text-slate-400 text-xs select-none"
              >
                ···
              </span>
            ) : (
              <button
                key={p}
                onClick={() => onPageChange(p)}
                className={cn(
                  'min-w-[36px] h-9 px-2 rounded-lg text-[12px] font-semibold transition-all border tabular-nums',
                  p === page
                    ? 'bg-blue-600 text-white border-blue-600 shadow-sm shadow-blue-200 scale-105'
                    : 'text-slate-600 border-slate-200 bg-white hover:bg-blue-50 hover:border-blue-300 hover:text-blue-600'
                )}
              >
                {p.toLocaleString()}
              </button>
            )
          )}
        </div>

        {/* Next */}
        <button
          disabled={page >= totalPages}
          onClick={() => onPageChange(page + 1)}
          title="次のページ"
          className={navBtnClass}
        >
          <span className="hidden sm:inline">次へ</span>
          <ChevronRight className="w-4 h-4" />
        </button>

        {/* Last page */}
        <button
          disabled={page >= totalPages}
          onClick={() => onPageChange(totalPages)}
          title="最後のページ"
          className={navBtnClass}
        >
          <ChevronsRight className="w-4 h-4" />
        </button>
      </div>

      {/* ── Info + jump row ──────────────── */}
      <div className="flex items-center gap-4 text-[12px] text-slate-500">
        <span>
          <strong className="text-slate-700">{page.toLocaleString()}</strong>
          {' / '}
          {totalPages.toLocaleString()} ページ
        </span>

        <div className="flex items-center gap-1.5">
          <span className="text-slate-400">移動:</span>
          <input
            type="text"
            value={jumpInput}
            onChange={(e) => setJumpInput(e.target.value.replace(/[^0-9]/g, ''))}
            onKeyDown={(e) => e.key === 'Enter' && handleJump()}
            placeholder="ページ番号"
            className="w-[90px] px-2.5 py-1 text-[12px] text-center border border-slate-200 rounded-lg bg-white focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
          />
          <button
            onClick={handleJump}
            className="px-3 py-1 text-[12px] font-medium text-white bg-blue-600 hover:bg-blue-700 rounded-lg transition-colors"
          >
            Go
          </button>
        </div>
      </div>
    </div>
  );
}

/* ── Shared nav button style ────────── */
const navBtnClass =
  'flex items-center gap-1 px-2.5 py-2 text-[12px] text-slate-600 rounded-lg border border-slate-200 bg-white hover:bg-slate-50 disabled:opacity-30 disabled:cursor-not-allowed transition-colors font-medium';
