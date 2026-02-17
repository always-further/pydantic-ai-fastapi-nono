// @ts-ignore
import { marked } from 'https://cdnjs.cloudflare.com/ajax/libs/marked/15.0.0/lib/marked.esm.js'

interface Message {
    role: 'user' | 'model' | 'tool'
    content: string
    timestamp: string
}

const convElement = document.getElementById('conversation') as HTMLDivElement
const spinner = document.getElementById('spinner') as HTMLDivElement
const form = document.getElementById('chat-form') as HTMLFormElement
const promptInput = document.getElementById('prompt') as HTMLInputElement

async function loadHistory(): Promise<void> {
    const response = await fetch('/chat/')
    if (response.ok) {
        const text = await response.text()
        if (text.trim()) {
            addMessages(text)
        }
    }
}

async function onFetchResponse(response: Response): Promise<void> {
    let text = ''
    const decoder = new TextDecoder()

    if (response.ok && response.body) {
        const reader = response.body.getReader()

        while (true) {
            const { done, value } = await reader.read()
            if (done) break

            text += decoder.decode(value)
            addMessages(text)
            spinner.classList.remove('active')
        }

        promptInput.disabled = false
        promptInput.focus()
    } else {
        spinner.classList.remove('active')
        promptInput.disabled = false
        console.error('Failed to send message:', response.statusText)
    }
}

function addMessages(responseText: string): void {
    const lines = responseText.split('\n')
    const messages: Message[] = lines
        .filter(line => line.trim().length > 0)
        .map(line => {
            try {
                return JSON.parse(line) as Message
            } catch {
                return null
            }
        })
        .filter((m): m is Message => m !== null)

    for (const message of messages) {
        const { timestamp, role, content } = message
        const id = `msg-${timestamp}`

        let msgDiv = document.getElementById(id)
        if (!msgDiv) {
            msgDiv = document.createElement('div')
            msgDiv.id = id
            msgDiv.classList.add(role)
            if (role === 'tool' && content.includes('BLOCKED')) {
                msgDiv.classList.add('blocked')
            }
            convElement.appendChild(msgDiv)
        }

        if (role === 'tool') {
            msgDiv.textContent = content
        } else {
            msgDiv.innerHTML = marked.parse(content) as string
        }
    }

    window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' })
}

async function handleSubmit(event: Event): Promise<void> {
    event.preventDefault()

    const prompt = promptInput.value.trim()
    if (!prompt) return

    promptInput.value = ''
    promptInput.disabled = true
    spinner.classList.add('active')

    const formData = new FormData()
    formData.append('prompt', prompt)

    try {
        const response = await fetch('/chat/', {
            method: 'POST',
            body: formData,
        })
        await onFetchResponse(response)
    } catch (error) {
        console.error('Error sending message:', error)
        spinner.classList.remove('active')
        promptInput.disabled = false
    }
}

form.addEventListener('submit', handleSubmit)

loadHistory()
