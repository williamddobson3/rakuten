import { Link, useLocation } from 'react-router-dom';
import { Search, TrendingUp, Bell, Activity } from 'lucide-react';

interface Props {
  multiplier: number;
  onMultiplierChange: (v: number) => void;
}

export default function Header({ multiplier, onMultiplierChange }: Props) {
  const { pathname } = useLocation();

  const navItems = [
    { to: '/', label: '商品検索', icon: Search },
    { to: '/rankings', label: '24hランキング', icon: TrendingUp },
    { to: '/watchlist', label: 'ウォッチリスト', icon: Bell },
  ];

  return (
    <header className="sticky top-0 z-50 bg-white border-b border-gray-200" style={{ boxShadow: '0 1px 3px rgba(0,0,0,0.06)' }}>
      <div className="max-w-screen-2xl mx-auto px-6 h-16 flex items-center gap-10">
        {/* Logo */}
        <Link to="/" className="flex items-center gap-3 shrink-0">
          <div className="w-9 h-9 rounded-lg flex items-center justify-center"
               style={{ background: 'linear-gradient(135deg, #3b82f6, #1d4ed8)' }}>
            <Activity className="w-5 h-5 text-white" />
          </div>
          <span className="text-xl font-extrabold text-gray-900 tracking-tight">
            PriceTrace
          </span>
        </Link>

        {/* Navigation */}
        <nav className="flex items-center gap-1">
          {navItems.map(({ to, label, icon: Icon }) => {
            const isActive =
              to === '/'
                ? pathname === '/' || pathname.startsWith('/product') || pathname.startsWith('/jan')
                : pathname === to;
            return (
              <Link
                key={to}
                to={to}
                className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-semibold transition-all duration-150 ${
                  isActive
                    ? 'text-blue-600 bg-blue-50'
                    : 'text-gray-500 hover:text-gray-900 hover:bg-gray-50'
                }`}
              >
                <Icon className="w-4 h-4" />
                {label}
              </Link>
            );
          })}
        </nav>

        {/* Spacer */}
        <div className="flex-1" />

        {/* Point Multiplier */}
        <div className="flex items-center gap-3 bg-gray-50 rounded-lg px-4 py-2 border border-gray-200">
          <span className="text-sm text-gray-500 whitespace-nowrap font-medium">
            ポイント倍率
          </span>
          <input
            type="number"
            min={1}
            max={100}
            value={multiplier}
            onChange={(e) => onMultiplierChange(Number(e.target.value))}
            className="w-14 text-center text-base font-bold text-gray-900 bg-white border border-gray-300 rounded-md px-1 py-1 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
          />
          <span className="text-sm text-gray-500 font-medium">倍</span>
        </div>

        {/* User avatar */}
        <div className="w-10 h-10 rounded-full flex items-center justify-center cursor-pointer"
             style={{ background: 'linear-gradient(135deg, #60a5fa, #2563eb)' }}>
          <span className="text-white text-sm font-bold">U</span>
        </div>
      </div>
    </header>
  );
}
