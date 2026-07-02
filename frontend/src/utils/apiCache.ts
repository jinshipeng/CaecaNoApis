const cache = new Map<string, { data: any; timestamp: number }>()

export function clearApiCache(key?: string): void {
  if (key) {
    cache.delete(key)
  } else {
    cache.clear()
  }
}
