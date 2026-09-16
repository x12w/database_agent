<script setup lang="ts">
import AdminView from './views/AdminView.vue'
import ChatView from './views/ChatView.vue'
import FixtureView from './views/FixtureView.vue'
import { route } from './router.ts'
</script>

<template>
  <ChatView v-if="route === '/'" />
  <AdminView v-else-if="route === '/admin'" />
  <FixtureView v-else />
</template>
