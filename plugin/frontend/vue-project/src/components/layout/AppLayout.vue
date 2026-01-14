<template>
  <el-container class="app-layout">
    <el-aside width="240px" class="sidebar-container">
      <Sidebar />
    </el-aside>
    <el-container>
      <div v-if="connectionStore.disconnected" class="connection-banner">
        <el-alert
          :title="t('common.disconnected')"
          type="error"
          :closable="false"
          show-icon
        />
      </div>
      <el-header height="60px" class="header-container">
        <Header />
      </el-header>
      <el-main class="main-container">
        <router-view v-slot="{ Component }">
          <transition name="fade" mode="out-in">
            <component :is="Component" />
          </transition>
        </router-view>
      </el-main>
    </el-container>
  </el-container>

  <el-dialog
    v-model="authDialogVisible"
    :close-on-click-modal="false"
    :close-on-press-escape="false"
    :show-close="false"
    :title="t('auth.login')"
    width="420px"
  >
    <el-form @submit.prevent="submitAuth">
      <el-form-item :label="t('auth.login')" :error="authError">
        <el-input
          v-model="authCodeInput"
          :placeholder="t('auth.codePlaceholder')"
          :maxlength="4"
          :disabled="authLoading"
          @keyup.enter="submitAuth"
          @input="handleAuthInput"
          autofocus
        />
      </el-form-item>

      <el-alert
        v-if="connectionStore.lastAuthErrorMessage"
        type="warning"
        :closable="false"
        show-icon
        :title="connectionStore.lastAuthErrorMessage || t('auth.reAuthRequired')"
        class="auth-warning"
      />

      <template #footer>
        <el-button :disabled="authLoading" @click="goToLogin">
          {{ t('auth.goToLogin') }}
        </el-button>
        <el-button type="primary" :loading="authLoading" :disabled="!isAuthCodeValid" @click="submitAuth">
          {{ t('auth.login') }}
        </el-button>
      </template>
    </el-form>
  </el-dialog>
</template>

<script setup lang="ts">
import { onMounted, ref, computed, watch } from 'vue'
import { useRouter } from 'vue-router'
import Sidebar from './Sidebar.vue'
import Header from './Header.vue'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import { useConnectionStore } from '@/stores/connection'
import { useAuthStore } from '@/stores/auth'
import { usePluginStore } from '@/stores/plugin'
import { get } from '@/api'

const { t } = useI18n()
const connectionStore = useConnectionStore()
const authStore = useAuthStore()
const pluginStore = usePluginStore()
const router = useRouter()

const authDialogVisible = ref(false)
const authCodeInput = ref('')
const authLoading = ref(false)
const authError = ref('')

const isAuthCodeValid = computed(() => /^[A-Z]{4}$/.test(authCodeInput.value.trim().toUpperCase()))

watch(
  () => connectionStore.authRequired,
  (required) => {
    authDialogVisible.value = required
    if (required) {
      authCodeInput.value = ''
      authError.value = ''
    }
  },
  { immediate: true }
)

function handleAuthInput() {
  authCodeInput.value = authCodeInput.value.toUpperCase()
  authError.value = ''
}

async function submitAuth() {
  if (!isAuthCodeValid.value) {
    authError.value = t('auth.codeError')
    return
  }

  authLoading.value = true
  authError.value = ''
  try {
    const normalized = authCodeInput.value.trim().toUpperCase()
    const ok = authStore.setAuthCode(normalized)
    if (!ok) {
      authError.value = t('auth.codeError')
      return
    }

    await get('/server/info')
    connectionStore.clearAuthRequired()
    authDialogVisible.value = false
    await pluginStore.fetchPlugins()
    ElMessage.success(t('auth.loginSuccess'))
  } catch (e: any) {
    authStore.clearAuthCode()
    authError.value = t('auth.codeError')
  } finally {
    authLoading.value = false
  }
}

function goToLogin() {
  authStore.clearAuthCode()
  connectionStore.clearAuthRequired()
  authDialogVisible.value = false
  router.push('/login')
}

onMounted(() => {
  console.log('✅ AppLayout component mounted')
})
</script>

<style scoped>
.app-layout {
  height: 100vh;
  overflow: hidden;
}

.sidebar-container {
  background-color: var(--el-bg-color);
  border-right: 1px solid var(--el-border-color-light);
}

.header-container {
  background-color: var(--el-bg-color);
  border-bottom: 1px solid var(--el-border-color-light);
  display: flex;
  align-items: center;
  padding: 0 20px;
}

.main-container {
  background-color: var(--el-bg-color-page);
  padding: 20px;
  overflow-y: auto;
}

.connection-banner {
  padding: 8px 20px 0 20px;
}

.auth-warning {
  margin-top: 8px;
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

