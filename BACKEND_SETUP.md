# Backend Setup Guide

This document describes the full-fledged backend implementation for the standalone SWE agent web dashboard.

## Architecture Overview

The application now includes a complete backend infrastructure with:

- **Database**: Neon PostgreSQL with Drizzle ORM
- **Authentication**: Better Auth (email + password)
- **Session Management**: JWT-based sessions with HTTP-only cookies
- **API Layer**: Express.js with RESTful endpoints
- **Frontend**: React with React Router for authentication flows

## Database Schema

The following tables are created in Neon:

### Authentication Tables (Better Auth)
- `user` - User accounts with email, name, and profile info
- `session` - Active user sessions
- `account` - OAuth provider accounts
- `verification` - Email verification tokens

### Application Tables
- `conversation` - Chat sessions with the SWE agent
- `message` - Individual messages within conversations
- `toolSchema` - Custom tool definitions managed by users
- `agentSession` - Active agent execution sessions

## Environment Variables

Required environment variables:

```bash
DATABASE_URL=postgresql://...         # Neon PostgreSQL connection string
BETTER_AUTH_SECRET=<random-string>    # Authentication secret (generate: openssl rand -base64 32)
SWE_AGENT_SERVER_TOKEN=<token>        # Token for Python agent communication
AGENT_SERVER_URL=http://localhost:8765 # Python SWE agent server URL
```

## API Endpoints

### Conversation Management

**POST /api/conversations**
Create a new conversation
```json
{
  "userId": "user-id",
  "title": "New conversation"
}
```

**GET /api/conversations?userId=user-id**
List all conversations for a user

**GET /api/conversations/:conversationId/messages?userId=user-id**
Fetch all messages in a conversation

**POST /api/conversations/:conversationId/messages**
Add a message to a conversation
```json
{
  "userId": "user-id",
  "role": "user",
  "content": "Your message content",
  "toolCalls": {}
}
```

### Agent Proxy Routes

**POST /api/chat**
Non-streaming chat request (proxied to Python agent)

**POST /api/chat/stream**
Streaming chat request using Server-Sent Events

### Tools & Config

**GET /api/tools**
Fetch tool schemas from Python agent

**POST /api/tools**
Update tool schemas (read-only from Python agent)

**POST /api/vscode/init**
Initialize VS Code workspace configuration

**GET /api/vscode/status**
Check VS Code workspace setup status

## Frontend Usage

### Authentication

Users must authenticate before accessing the dashboard. The frontend provides:

- **Sign In page** (`/sign-in`) - Login with email and password
- **Sign Up page** (`/sign-up`) - Create a new account
- **Protected Routes** - Dashboard routes require authentication

### Hooks

#### `useAuth()`
Access user authentication state:
```typescript
const { user, isAuthenticated, isLoading, signOut } = useAuth()
```

#### `useConversation(conversationId, userId)`
Manage conversation persistence:
```typescript
const {
  messages,
  conversation,
  loading,
  error,
  createConversation,
  addMessage,
  loadConversation
} = useConversation()
```

### Authentication Flow

1. User visits the dashboard
2. If not authenticated, redirected to `/sign-in`
3. After successful authentication, user can access main dashboard
4. Conversations and messages are automatically saved to database
5. User can sign out to return to login screen

## Development Setup

1. **Install dependencies**:
   ```bash
   cd web
   npm install
   ```

2. **Set environment variables**:
   ```bash
   export DATABASE_URL="your-neon-connection-string"
   export BETTER_AUTH_SECRET=$(openssl rand -base64 32)
   export SWE_AGENT_SERVER_TOKEN="your-agent-token"
   ```

3. **Start the development server**:
   ```bash
   npm run dev
   ```

The Express server will start on port 3000 and proxy requests to the Python SWE agent.

## Security Considerations

- **Session Management**: Better Auth handles secure session creation and validation
- **Password Hashing**: All passwords are hashed using bcrypt
- **HTTP-Only Cookies**: Session tokens are stored in HTTP-only cookies to prevent XSS attacks
- **CORS**: The backend runs on the same origin as the frontend, eliminating CORS issues
- **User Scoping**: All API endpoints verify user ownership before returning data
- **Token Validation**: The Python agent token is kept server-side and never exposed to the browser

## Data Persistence

All conversations and messages are persisted to the database automatically:

- New conversations are created when users start a chat session
- Messages are saved as they're sent/received
- Tool schemas are stored per-user for customization
- Sessions are stored with expiration times for auto-logout

## Troubleshooting

### "Unauthorized" errors
- Verify `BETTER_AUTH_SECRET` is set in environment variables
- Check that the user session cookie is being stored

### Database connection errors
- Confirm `DATABASE_URL` is valid and accessible
- Verify the Neon database is running and network-accessible

### Authentication not working in development
- Ensure `NODE_ENV` is set correctly (development mode enables special cookie settings)
- Clear browser cookies and try again
- Check browser console for CORS or cookie errors

## Next Steps

Future enhancements could include:

- OAuth provider integration (Google, GitHub)
- Team collaboration and conversation sharing
- Message search and filtering
- Advanced conversation management (export, delete, organize)
- Rate limiting and usage analytics
- Audit logging for compliance
