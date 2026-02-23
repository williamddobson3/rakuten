import { Activity } from 'lucide-react';

export default function Footer() {
  return (
    <footer className="bg-white border-t border-slate-200 mt-auto">
      <div className="max-w-[1480px] mx-auto px-5 py-4 flex items-center justify-between">
        <div className="flex items-center gap-2.5">
          <Activity className="w-4 h-4 text-blue-500" />
          <span className="text-[13px] font-bold text-slate-500">
            PriceTrace
          </span>
          <span className="text-[12px] text-slate-400">
            © 2024 価格追跡・分析ツール
          </span>
        </div>
        <div className="flex items-center gap-5 text-[12px] text-slate-400">
          <span className="hover:text-slate-600 cursor-pointer transition-colors">
            利用規約
          </span>
          <span className="hover:text-slate-600 cursor-pointer transition-colors">
            プライバシーポリシー
          </span>
          <span className="hover:text-slate-600 cursor-pointer transition-colors">
            お問い合わせ
          </span>
          <span className="flex items-center gap-1.5">
            <span className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse" />
            <span className="text-slate-500 font-medium">システム正常稼働中</span>
          </span>
        </div>
      </div>
    </footer>
  );
}
