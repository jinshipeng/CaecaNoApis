import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

export const useLoadingStore = defineStore('loading', () => {
  const loadingCount = ref(0)
  const loadingText = ref('加载中...')
  const loadingTargets = ref<Set<string>>(new Set())

  const isLoading = computed(() => loadingCount.value > 0)

  const startLoading = (target?: string, text?: string) => {
    loadingCount.value++
    if (target) {
      loadingTargets.value.add(target)
    }
    if (text) {
      loadingText.value = text
    }
  }

  const stopLoading = (target?: string) => {
    if (loadingCount.value > 0) {
      loadingCount.value--
    }
    if (target) {
      loadingTargets.value.delete(target)
    }
  }

  const setLoadingText = (text: string) => {
    loadingText.value = text
  }

  const isTargetLoading = (target: string) => {
    return loadingTargets.value.has(target)
  }

  const resetLoading = () => {
    loadingCount.value = 0
    loadingTargets.value.clear()
    loadingText.value = '加载中...'
  }

  return {
    loadingCount,
    loadingText,
    loadingTargets,
    isLoading,
    startLoading,
    stopLoading,
    setLoadingText,
    isTargetLoading,
    resetLoading
  }
})