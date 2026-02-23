import { Link } from 'react-router-dom';
import {
  Bell,
  Trash2,
  ExternalLink,
  Tag,
  PackageOpen,
} from 'lucide-react';
import type { WatchlistItem } from '../lib/types';
import { relativeTime } from '../lib/utils';

interface Props {
  items: WatchlistItem[];
  removeWatch: (uid: string) => void;
}

export default function WatchlistPage({ items, removeWatch }: Props) {
  return (
    <div className="max-w-[900px] mx-auto px-4 py-6">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-xl font-bold text-slate-800 flex items-center gap-2">
            <Bell className="w-6 h-6 text-amber-500" />
            ウォッチリスト
          </h1>
          <p className="text-sm text-slate-500 mt-1">
            登録した商品の価格変動を追跡します（{items.length}件登録中）
          </p>
        </div>
      </div>

      {/* Content */}
      {items.length === 0 ? (
        <div className="text-center py-20">
          <PackageOpen className="w-14 h-14 text-slate-300 mx-auto mb-4" />
          <p className="text-lg font-medium text-slate-500 mb-2">
            ウォッチリストは空です
          </p>
          <p className="text-sm text-slate-400 mb-4">
            商品検索ページから気になる商品をウォッチリストに追加してください
          </p>
          <Link
            to="/"
            className="inline-flex items-center gap-2 px-5 py-2.5 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700 transition-colors"
          >
            商品を検索する
          </Link>
        </div>
      ) : (
        <div className="space-y-2">
          {items.map((item) => (
            <div
              key={item.product_uid}
              className="group bg-white rounded-xl border border-slate-200 hover:border-blue-200 hover:shadow-md transition-all p-4 flex items-center gap-4"
            >
              {/* Image */}
              <div className="shrink-0 w-14 h-14 rounded-lg overflow-hidden bg-slate-100 border border-slate-200">
                {item.image_url ? (
                  <img
                    src={item.image_url}
                    alt={item.item_name}
                    className="w-full h-full object-cover"
                    loading="lazy"
                  />
                ) : (
                  <div className="w-full h-full flex items-center justify-center text-slate-300">
                    <Tag className="w-6 h-6" />
                  </div>
                )}
              </div>

              {/* Info */}
              <div className="flex-1 min-w-0">
                <Link
                  to={`/product/${item.product_uid}`}
                  className="text-sm font-medium text-slate-800 hover:text-blue-600 transition-colors line-clamp-1"
                >
                  {item.item_name || item.product_uid}
                </Link>
                <div className="flex items-center gap-3 mt-1 text-[11px] text-slate-400">
                  <span className="font-mono">{item.product_uid}</span>
                  <span>追加: {relativeTime(item.added_at)}</span>
                </div>
              </div>

              {/* Actions */}
              <div className="flex items-center gap-2">
                <Link
                  to={`/product/${item.product_uid}`}
                  className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-blue-600 bg-blue-50 rounded-lg hover:bg-blue-100 transition-colors"
                >
                  <ExternalLink className="w-3.5 h-3.5" />
                  詳細
                </Link>
                <button
                  onClick={() => removeWatch(item.product_uid)}
                  className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-red-500 bg-red-50 rounded-lg hover:bg-red-100 transition-colors"
                >
                  <Trash2 className="w-3.5 h-3.5" />
                  削除
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
