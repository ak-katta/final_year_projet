from llm import llm

for chunk in llm.stream("Explain recursion briefly."):
    print(chunk, end="", flush=True)