<template>
  <div class="dashboard-page database-page" :class="{ 'is-dark': isDark }">
    <v-container fluid class="dashboard-shell pa-4 pa-md-6">
      <div class="dashboard-header">
        <div class="dashboard-header-main">
          <h1 class="dashboard-title">{{ tm('page.title') }}</h1>
          <p class="dashboard-subtitle">{{ tm('page.subtitle') }}</p>
        </div>
        <div class="dashboard-header-actions">
          <v-btn variant="text" color="primary" prepend-icon="mdi-refresh" :loading="loading" @click="reload">
            {{ tm('actions.refresh') }}
          </v-btn>
          <v-btn variant="tonal" color="primary" prepend-icon="mdi-content-save" :loading="saving" @click="save">
            {{ tm('actions.save') }}
          </v-btn>
        </div>
      </div>

      <v-alert
        v-if="restartRequired"
        type="warning"
        variant="tonal"
        density="comfortable"
        class="mb-4"
      >
        {{ tm('messages.restartRequired') }}
      </v-alert>

      <section class="dashboard-card dashboard-card--padded mb-4">
        <div class="dashboard-section-title">{{ tm('sections.backend') }}</div>
        <div class="dashboard-section-subtitle">{{ tm('sections.backendHint') }}</div>

        <v-table density="comfortable" class="mt-3 db-status-table">
          <tbody>
            <tr>
              <td class="key">{{ tm('status.currentDialect') }}</td>
              <td>{{ live.dialect || '-' }}</td>
            </tr>
            <tr>
              <td class="key">{{ tm('status.currentUrl') }}</td>
              <td class="mono">{{ live.url || '-' }}</td>
            </tr>
            <tr>
              <td class="key">{{ tm('status.memoryTables') }}</td>
              <td>{{ (live.memory_tables || []).join(', ') || '-' }}</td>
            </tr>
          </tbody>
        </v-table>
      </section>

      <section class="dashboard-card dashboard-card--padded">
        <div class="dashboard-section-title">{{ tm('sections.settings') }}</div>
        <div class="dashboard-section-subtitle">{{ tm('sections.settingsHint') }}</div>

        <v-select
          v-model="form.type"
          :items="typeOptions"
          item-title="label"
          item-value="value"
          :label="tm('fields.type')"
          variant="outlined"
          density="comfortable"
          hide-details="auto"
          class="mt-3"
          @update:model-value="onTypeChange"
        />

        <div v-if="form.type === 'mysql'" class="mysql-grid mt-2">
          <v-text-field
            v-model="form.mysql_host"
            :label="tm('fields.host')"
            variant="outlined"
            density="comfortable"
            hide-details="auto"
          />
          <v-text-field
            v-model.number="form.mysql_port"
            :label="tm('fields.port')"
            type="number"
            variant="outlined"
            density="comfortable"
            hide-details="auto"
          />
          <v-text-field
            v-model="form.mysql_user"
            :label="tm('fields.user')"
            variant="outlined"
            density="comfortable"
            hide-details="auto"
          />
          <v-text-field
            v-model="form.mysql_password"
            :label="tm('fields.password')"
            type="password"
            variant="outlined"
            density="comfortable"
            hide-details="auto"
            :placeholder="hasStoredPassword ? tm('fields.passwordKeepHint') : ''"
          />
          <v-text-field
            v-model="form.mysql_database"
            :label="tm('fields.database')"
            variant="outlined"
            density="comfortable"
            hide-details="auto"
          />
          <v-text-field
            v-model="form.mysql_charset"
            :label="tm('fields.charset')"
            variant="outlined"
            density="comfortable"
            hide-details="auto"
          />
        </div>

        <div class="d-flex flex-wrap ga-2 mt-4">
          <v-btn
            v-if="form.type === 'mysql'"
            variant="tonal"
            color="primary"
            prepend-icon="mdi-lan-connect"
            :loading="testing"
            @click="testConnection"
          >
            {{ tm('actions.test') }}
          </v-btn>
          <v-btn variant="tonal" color="primary" prepend-icon="mdi-content-save" :loading="saving" @click="save">
            {{ tm('actions.save') }}
          </v-btn>
        </div>

        <v-alert
          v-if="testResult"
          :type="testResult.ok ? 'success' : 'error'"
          variant="tonal"
          density="comfortable"
          class="mt-3"
        >
          {{ testResult.message }}
        </v-alert>
      </section>
    </v-container>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useTheme } from 'vuetify'
import { useModuleI18n } from '@/i18n/composables'
import { useToast } from '@/utils/toast'

const theme = useTheme()
const isDark = computed(() => theme.global.current.value.dark)
const { tm } = useModuleI18n('features/database')
const toast = useToast()

const loading = ref(false)
const saving = ref(false)
const testing = ref(false)
const restartRequired = ref(false)
const hasStoredPassword = ref(false)

const live = reactive<{ dialect: string; url: string; memory_tables: string[] }>({
  dialect: '',
  url: '',
  memory_tables: [],
})
const form = reactive<{
  type: string
  mysql_host: string
  mysql_port: number
  mysql_user: string
  mysql_password: string
  mysql_database: string
  mysql_charset: string
}>({
  type: 'sqlite',
  mysql_host: '',
  mysql_port: 3306,
  mysql_user: '',
  mysql_password: '',
  mysql_database: '',
  mysql_charset: 'utf8mb4',
})

const typeOptions = computed(() => [
  { value: 'sqlite', label: tm('types.sqlite') },
  { value: 'mysql', label: tm('types.mysql') },
])

type DbPayload = { status?: string; message?: string; data?: unknown }

const api = {
  async request<T = Record<string, unknown>>(
    method: string,
    path: string,
    body?: Record<string, unknown>,
  ): Promise<T> {
    const response = await fetch(`/api/v1/database${path}`, {
      method,
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: body === undefined ? undefined : JSON.stringify(body),
    })
    const payload = (await response.json().catch(() => ({}))) as DbPayload
    if (!response.ok || payload?.status === 'error') {
      throw new Error(payload?.message || `HTTP ${response.status}`)
    }
    return (payload?.data ?? payload) as T
  },
}

type DbSettings = Partial<{
  type: string
  mysql_host: string
  mysql_port: number
  mysql_user: string
  mysql_database: string
  mysql_charset: string
  mysql_password_set: boolean
}>
type DbLive = Partial<{ dialect: string; url: string; memory_tables: string[]; restart_required: boolean }>
type DbResponse = { settings?: DbSettings; live?: DbLive; message?: string }

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

async function reload() {
  loading.value = true
  try {
    const data = await api.request<DbResponse>('GET', '')
    const settings = data.settings || {}
    Object.assign(form, {
      type: settings.type || 'sqlite',
      mysql_host: settings.mysql_host || '',
      mysql_port: settings.mysql_port || 3306,
      mysql_user: settings.mysql_user || '',
      mysql_password: '',
      mysql_database: settings.mysql_database || '',
      mysql_charset: settings.mysql_charset || 'utf8mb4',
    })
    hasStoredPassword.value = Boolean(settings.mysql_password_set)
    Object.assign(live, data.live || {})
    restartRequired.value = Boolean(data.live?.restart_required)
  } catch (error: unknown) {
    toast.error(errorMessage(error))
  } finally {
    loading.value = false
  }
}

function onTypeChange() {
  if (form.type === 'sqlite') {
    testResult.value = null
  }
}

async function save() {
  saving.value = true
  try {
    const payload: Record<string, unknown> = {
      type: form.type,
      mysql_host: form.mysql_host,
      mysql_port: Number(form.mysql_port) || 3306,
      mysql_user: form.mysql_user,
      mysql_database: form.mysql_database,
      mysql_charset: form.mysql_charset || 'utf8mb4',
    }
    if (form.mysql_password) {
      payload.mysql_password = form.mysql_password
    }
    await api.request('PUT', '', payload)
    toast.success(tm('messages.saved'))
    await reload()
  } catch (error: unknown) {
    toast.error(errorMessage(error))
  } finally {
    saving.value = false
  }
}

const testResult = ref<{ ok: boolean; message: string } | null>(null)

async function testConnection() {
  testing.value = true
  testResult.value = null
  try {
    const payload: Record<string, unknown> = {
      mysql_host: form.mysql_host,
      mysql_port: Number(form.mysql_port) || 3306,
      mysql_user: form.mysql_user,
      mysql_database: form.mysql_database,
      mysql_charset: form.mysql_charset || 'utf8mb4',
    }
    if (form.mysql_password) {
      payload.mysql_password = form.mysql_password
    }
    const data = await api.request<{ message?: string }>('POST', '/test', payload)
    testResult.value = { ok: true, message: data.message || tm('messages.testOk') }
  } catch (error: unknown) {
    testResult.value = { ok: false, message: errorMessage(error) }
  } finally {
    testing.value = false
  }
}

onMounted(reload)
</script>

<style scoped>
.database-page {
  min-height: 100%;
}

.db-status-table .key {
  width: 220px;
  color: rgb(var(--v-theme-on-surface-variant));
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  word-break: break-all;
}

.mysql-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 12px;
}
</style>
