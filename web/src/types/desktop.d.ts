export {}
declare global {
  interface Window {
    desktop?: {
      isDesktop: true
      restart: () => Promise<void>
      openDataDirectory: () => Promise<string>
      chooseKnowledgeFile: () => Promise<string | null>
    }
  }
}
