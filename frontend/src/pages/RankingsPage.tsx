import { useState, useEffect } from 'react';
import {
  TrendingUp,
  Trophy,
  Store,
  RefreshCw,
  Loader2,
  Calendar,
  Clock,
  BarChart3,
} from 'lucide-react';
import { getLiveRankings, getDailyRankings } from '../api/client';
import type { ShopRankEntry } from '../lib/types';
import { cn } from '../lib/utils';

type TabType = 'live' | 'daily';

export default function RankingsPage() {
  const [tab, setTab] = useState<TabType>('live');
  const [rankings, setRankings] = useState<ShopRankEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [hours, setHours] = useState(24);
  const [dailyDate, setDailyDate] = useState('');

  const fetchRankings = async () => {
    setLoading(true);
    setError(null);
    try {
      if (tab === 'live') {
        const res = await getLiveRankings(50, hours);
        setRankings(res.rankings);
      } else {
        const res = await getDailyRankings(dailyDate || undefined, 50);
        setRankings(res.rankings);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load rankings');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchRankings();
  }, [tab, hours, dailyDate]);

  const maxCount = rankings.length > 0 ? rankings[0].browse_count : 1;

  return (
    <div className="max-w-[1000px] mx-auto px-4 py-6">
      {/* Page header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-xl font-bold text-slate-800 flex items-center gap-2">
            <TrendingUp className="w-6 h-6 text-blue-600" />
            店舗アクセスランキング
          </h1>
          <p className="text-sm text-slate-500 mt-1">
            拡張機能ユーザーの閲覧データに基づく店舗コード頻度ランキング
          </p>
        </div>
        <button
          onClick={fetchRankings}
          disabled={loading}
          className="flex items-center gap-2 px-4 py-2 bg-white border border-slate-200 rounded-lg text-sm text-slate-600 hover:bg-slate-50 transition-colors"
        >
          <RefreshCw className={cn('w-4 h-4', loading && 'animate-spin')} />
          更新
        </button>
      </div>

      {/* Tabs */}
      <div className="flex items-center gap-4 mb-6">
        <div className="flex bg-slate-100 rounded-lg p-0.5">
          <button
            onClick={() => setTab('live')}
            className={cn(
              'flex items-center gap-1.5 px-4 py-2 rounded-md text-sm font-medium transition-colors',
              tab === 'live'
                ? 'bg-white text-blue-600 shadow-sm'
                : 'text-slate-500 hover:text-slate-700'
            )}
          >
            <Clock className="w-4 h-4" />
            リアルタイム
          </button>
          <button
            onClick={() => setTab('daily')}
            className={cn(
              'flex items-center gap-1.5 px-4 py-2 rounded-md text-sm font-medium transition-colors',
              tab === 'daily'
                ? 'bg-white text-blue-600 shadow-sm'
                : 'text-slate-500 hover:text-slate-700'
            )}
          >
            <Calendar className="w-4 h-4" />
            日次集計
          </button>
        </div>

        {tab === 'live' && (
          <select
            value={hours}
            onChange={(e) => setHours(Number(e.target.value))}
            className="text-sm border border-slate-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white"
          >
            <option value={6}>過去6時間</option>
            <option value={12}>過去12時間</option>
            <option value={24}>過去24時間</option>
            <option value={48}>過去48時間</option>
            <option value={168}>過去7日間</option>
          </select>
        )}

        {tab === 'daily' && (
          <input
            type="date"
            value={dailyDate}
            onChange={(e) => setDailyDate(e.target.value)}
            className="text-sm border border-slate-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white"
          />
        )}
      </div>

      {/* Content */}
      {loading ? (
        <div className="flex items-center justify-center py-20">
          <Loader2 className="w-8 h-8 text-blue-500 animate-spin" />
        </div>
      ) : error ? (
        <div className="bg-red-50 border border-red-200 rounded-xl p-4 text-sm text-red-700">
          {error}
        </div>
      ) : rankings.length === 0 ? (
        <div className="text-center py-20 text-slate-400">
          <BarChart3 className="w-12 h-12 mx-auto mb-3 text-slate-300" />
          <p className="text-sm">ランキングデータがありません</p>
        </div>
      ) : (
        <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-slate-50 border-b border-slate-200">
                <th className="text-center py-3 px-4 text-xs font-medium text-slate-500 w-16">
                  順位
                </th>
                <th className="text-left py-3 px-4 text-xs font-medium text-slate-500">
                  店舗
                </th>
                <th className="text-right py-3 px-4 text-xs font-medium text-slate-500 w-24">
                  閲覧数
                </th>
                <th className="text-left py-3 px-4 text-xs font-medium text-slate-500 w-[40%]">
                  分布
                </th>
              </tr>
            </thead>
            <tbody>
              {rankings.map((entry) => (
                <tr
                  key={entry.shop_code}
                  className="border-b border-slate-100 hover:bg-slate-50 transition-colors"
                >
                  <td className="py-3 px-4 text-center">
                    {entry.rank <= 3 ? (
                      <span
                        className={cn(
                          'inline-flex items-center justify-center w-7 h-7 rounded-full text-xs font-bold',
                          entry.rank === 1 && 'bg-amber-100 text-amber-700',
                          entry.rank === 2 && 'bg-slate-200 text-slate-700',
                          entry.rank === 3 && 'bg-orange-100 text-orange-700'
                        )}
                      >
                        <Trophy className="w-3.5 h-3.5" />
                      </span>
                    ) : (
                      <span className="text-slate-500 font-medium">
                        {entry.rank}
                      </span>
                    )}
                  </td>
                  <td className="py-3 px-4">
                    <div className="flex items-center gap-2.5">
                      <div className="w-8 h-8 bg-slate-100 rounded-lg flex items-center justify-center">
                        <Store className="w-4 h-4 text-slate-400" />
                      </div>
                      <div>
                        <div className="font-medium text-slate-700">
                          {entry.shop_name ?? entry.shop_code}
                        </div>
                        <div className="text-[11px] text-slate-400 font-mono">
                          {entry.shop_code}
                        </div>
                      </div>
                    </div>
                  </td>
                  <td className="py-3 px-4 text-right font-bold text-slate-700">
                    {entry.browse_count.toLocaleString()}
                  </td>
                  <td className="py-3 px-4">
                    <div className="flex items-center gap-2">
                      <div className="flex-1 h-2 bg-slate-100 rounded-full overflow-hidden">
                        <div
                          className={cn(
                            'h-full rounded-full transition-all',
                            entry.rank === 1
                              ? 'bg-gradient-to-r from-blue-500 to-blue-600'
                              : entry.rank <= 3
                              ? 'bg-blue-400'
                              : 'bg-blue-300'
                          )}
                          style={{
                            width: `${(entry.browse_count / maxCount) * 100}%`,
                          }}
                        />
                      </div>
                      <span className="text-[10px] text-slate-400 w-8 text-right">
                        {Math.round((entry.browse_count / maxCount) * 100)}%
                      </span>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
