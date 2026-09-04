import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '@/api'
import type { User } from '@/types'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<User | null>(null)
  const initialized = ref(false)

  async function initialize() {
    if (initialized.value) return
    try {
      user.value = await api.me()
    } catch {
      user.value = null
    } finally {
      initialized.value = true
    }
  }

  async function refresh() {
    try {
      user.value = await api.me()
    } catch {
      user.value = null
    } finally {
      initialized.value = true
    }
  }

  function setUser(nextUser: User) {
    user.value = nextUser
    initialized.value = true
  }

  async function login(email: string, password: string) {
    user.value = await api.login({ email, password })
    initialized.value = true
  }

  async function logout() {
    try {
      await api.logout()
    } finally {
      user.value = null
      initialized.value = true
    }
  }

  return { user, initialized, initialize, refresh, setUser, login, logout }
})
