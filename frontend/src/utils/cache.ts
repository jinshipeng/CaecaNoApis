interface CacheItem {
  data: any
  timestamp: number
  ttl: number
}

const cache = new Map<string, CacheItem>()

export function getCache(key: string): any | null {
  const item = cache.get(key)
  if (!item) return null
  
  if (Date.now() - item.timestamp > item.ttl) {
    cache.delete(key)
    return null
  }
  
  return item.data
}

export function setCache(key: string, data: any, ttl: number = 300000): void {
  cache.set(key, {
    data,
    timestamp: Date.now(),
    ttl
  })
}

