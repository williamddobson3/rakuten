import { Outlet } from 'react-router-dom';
import Header from './Header';
import Footer from './Footer';

interface Props {
  multiplier: number;
  onMultiplierChange: (v: number) => void;
}

export default function Layout({ multiplier, onMultiplierChange }: Props) {
  return (
    <div className="min-h-screen flex flex-col">
      <Header multiplier={multiplier} onMultiplierChange={onMultiplierChange} />
      <main className="flex-1">
        <Outlet />
      </main>
      <Footer />
    </div>
  );
}
