<script setup lang="ts">
import { useLoadingStore } from '@/stores/loading'
import { Loading } from '@element-plus/icons-vue'

const loadingStore = useLoadingStore()
</script>

<template>
  <Teleport to="body">
    <Transition name="fade">
      <div
        v-if="loadingStore.isLoading"
        class="loading-overlay"
      >
        <div class="loading-content">
          <div class="loading-spinner">
            <Loading class="loading-icon" />
          </div>
          <span class="loading-text">{{ loadingStore.loadingText }}</span>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.loading-overlay {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 9999;
  backdrop-filter: blur(4px);
}

.loading-content {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 16px;
  padding: 32px 48px;
  background: linear-gradient(135deg, rgba(30, 30, 46, 0.95), rgba(40, 40, 60, 0.95));
  border-radius: 16px;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5);
  border: 1px solid rgba(110, 158, 247, 0.2);
}

.loading-spinner {
  width: 56px;
  height: 56px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.loading-icon {
  font-size: 48px;
  color: #6E9EF7;
  animation: spin 1s linear infinite;
}

@keyframes spin {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}

.loading-text {
  font-size: 14px;
  color: #a8adb5;
  font-weight: 500;
}

.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.3s ease;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>