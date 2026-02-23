import { BrowserRouter, Routes, Route } from 'react-router-dom';
import Layout from './components/Layout';
import SearchPage from './pages/SearchPage';
import ProductDetailPage from './pages/ProductDetailPage';
import RankingsPage from './pages/RankingsPage';
import WatchlistPage from './pages/WatchlistPage';
import JanSearchPage from './pages/JanSearchPage';
import { usePointMultiplier } from './hooks/usePointMultiplier';
import { useWatchlist } from './hooks/useWatchlist';

export default function App() {
  const { multiplier, setMultiplier } = usePointMultiplier();
  const { items: watchItems, add: addWatch, remove: removeWatch, isWatching } = useWatchlist();

  return (
    <BrowserRouter>
      <Routes>
        <Route
          element={
            <Layout
              multiplier={multiplier}
              onMultiplierChange={setMultiplier}
            />
          }
        >
          <Route
            index
            element={
              <SearchPage
                multiplier={multiplier}
                watchlist={watchItems}
                isWatching={isWatching}
                addWatch={addWatch}
                removeWatch={removeWatch}
              />
            }
          />
          <Route
            path="/product/*"
            element={
              <ProductDetailPage
                multiplier={multiplier}
                isWatching={isWatching}
                addWatch={addWatch}
                removeWatch={removeWatch}
              />
            }
          />
          <Route
            path="/jan/:janCode"
            element={<JanSearchPage multiplier={multiplier} />}
          />
          <Route path="/rankings" element={<RankingsPage />} />
          <Route
            path="/watchlist"
            element={
              <WatchlistPage items={watchItems} removeWatch={removeWatch} />
            }
          />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
