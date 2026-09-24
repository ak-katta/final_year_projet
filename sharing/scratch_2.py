from openai import OpenAI

api_key = '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'

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=api_key,
)

completion = client.chat.completions.create(
    model="~openai/gpt-sol-latest",
    messages=[
        {
            "role": "user",
            "content": "can you can me? "
        }
    ],
    max_tokens=1000
)

print(completion.choices[0].message.content)
