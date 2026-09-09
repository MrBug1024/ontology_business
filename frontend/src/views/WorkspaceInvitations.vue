<template>
  <div class="access-page"><header class="access-heading"><div><span class="access-eyebrow">WORKSPACE INVITATIONS</span><h1>工作区邀请</h1><p>查看发送至 {{ auth.user?.email }} 的协作邀请，同意后即可切换到对应工作区。</p></div></header><section class="access-card"><InvitationsPanel inbox /></section></div>
</template>

<script setup lang="ts">
import InvitationsPanel from '@/components/access/InvitationsPanel.vue'
import { useAuthStore } from '@/stores/auth'
import '@/styles/access.css'
const auth = useAuthStore()
</script>
