import { useState, useCallback } from 'react';
import type { WatchlistItem } from '../lib/types';

const STORAGE_KEY = 'pt_watchlist';

function load(): WatchlistItem[] {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]');
  } catch {
    return [];
  }
}

function save(items: WatchlistItem[]) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(items));
}

export function useWatchlist() {
  const [items, setItems] = useState<WatchlistItem[]>(load);

  const add = useCallback((item: WatchlistItem) => {
    setItems(prev => {
      if (prev.some(i => i.product_uid === item.product_uid)) return prev;
      const next = [item, ...prev];
      save(next);
      return next;
    });
  }, []);

  const remove = useCallback((uid: string) => {
    setItems(prev => {
      const next = prev.filter(i => i.product_uid !== uid);
      save(next);
      return next;
    });
  }, []);

  const isWatching = useCallback((uid: string) => {
    return items.some(i => i.product_uid === uid);
  }, [items]);

  return { items, add, remove, isWatching };
}
