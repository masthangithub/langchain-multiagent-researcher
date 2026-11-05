from src.tools.tools import web_search
from src.tools.tools import scrape_webpage
from rich import print
# results =web_search("current news on bio robotics")
# print(results)

""" The @tool decorator turned the function into a Langchain tool object, and those 
arent called like normal functions. call it with .invoke() and a dictionary """
webcontent = scrape_webpage.invoke({"url": "https://www.nature.com/articles/s44182-025-00056-x"})   # recommended
# webcontent = scrape_webpage.invoke( "https://www.nature.com/articles/s44182-025-00056-x")
print(webcontent)