import React, { useEffect, useState } from 'react'
import { useAuth } from '../lib/hooks/useAuth'
import { useConversation } from '../lib/hooks/useConversation'
import { CodingMode } from './CodingMode'
import { Plus, MessageCircle } from 'lucide-react'

interface StoredConversation {
  id: string
  userId: string
  title: string
  createdAt: string
  updatedAt: string
}

export function ConversationManager() {
  const { user } = useAuth()
  const [conversations, setConversations] = useState<StoredConversation[]>([])
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const { createConversation } = useConversation()

  // Load conversations on mount
  useEffect(() => {
    if (user?.id) {
      loadConversations()
    }
  }, [user?.id])

  const loadConversations = async () => {
    if (!user?.id) return
    try {
      setLoading(true)
      const response = await fetch(`/api/conversations?userId=${user.id}`)
      if (response.ok) {
        const data = await response.json()
        setConversations(data)
        if (data.length > 0 && !activeConversationId) {
          setActiveConversationId(data[0].id)
        }
      }
    } catch (err) {
      console.error('[v0] Error loading conversations:', err)
    } finally {
      setLoading(false)
    }
  }

  const handleNewConversation = async () => {
    if (!user?.id) return
    try {
      const title = `Conversation ${new Date().toLocaleTimeString()}`
      const conversationId = await createConversation(user.id, title)
      if (conversationId) {
        setActiveConversationId(conversationId)
        await loadConversations()
      }
    } catch (err) {
      console.error('[v0] Error creating conversation:', err)
    }
  }

  const activeConversation = conversations.find((c) => c.id === activeConversationId)

  return (
    <div className="flex h-screen w-full">
      {/* Conversation Sidebar */}
      <aside className="w-64 border-r border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 flex flex-col">
        <div className="p-4 border-b border-slate-200 dark:border-slate-800">
          <button
            onClick={handleNewConversation}
            className="w-full flex items-center gap-2 px-4 py-2 bg-primary text-primary-foreground rounded-lg font-medium hover:bg-primary/90 transition"
          >
            <Plus className="h-4 w-4" />
            New Chat
          </button>
        </div>

        <div className="flex-1 overflow-y-auto">
          {loading ? (
            <div className="p-4 text-center text-muted-foreground text-sm">Loading...</div>
          ) : conversations.length === 0 ? (
            <div className="p-4 text-center text-muted-foreground text-sm">No conversations yet</div>
          ) : (
            <div className="space-y-1 p-2">
              {conversations.map((conv) => (
                <button
                  key={conv.id}
                  onClick={() => setActiveConversationId(conv.id)}
                  className={`w-full text-left px-3 py-2 rounded-lg text-sm transition-colors flex items-center gap-2 ${
                    activeConversationId === conv.id
                      ? 'bg-primary/10 text-primary'
                      : 'text-foreground hover:bg-slate-100 dark:hover:bg-slate-800'
                  }`}
                >
                  <MessageCircle className="h-4 w-4 flex-shrink-0" />
                  <span className="truncate">{conv.title}</span>
                </button>
              ))}
            </div>
          )}
        </div>
      </aside>

      {/* Main Chat Area */}
      <main className="flex-1">
        {activeConversation && user ? (
          <CodingMode
            key={activeConversation.id}
            conversationId={activeConversation.id}
            userId={user.id}
          />
        ) : (
          <div className="flex h-full items-center justify-center">
            <div className="text-center">
              <MessageCircle className="h-12 w-12 mx-auto text-muted-foreground mb-4" />
              <p className="text-muted-foreground">Start a new conversation to begin</p>
            </div>
          </div>
        )}
      </main>
    </div>
  )
}
