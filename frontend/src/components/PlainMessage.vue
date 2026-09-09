<template><div class="plain-message">{{ text }}</div></template>

<script setup lang="ts">
import { computed } from 'vue'
import { plainMessage } from '@/utils/plainMessage'
const props = defineProps<{ content: string }>()
const text = computed(() => plainMessage(props.content))
</script>

<style scoped>
.plain-message { white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.65; min-width: 0; }
</style>
