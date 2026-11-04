scriptName TestPapyrusScript extends Quest

Import F4SE_HTTP
Import F4SE

function OnHttpReplyReceived(int typedDictionaryHandle)    
    string replyType = F4SE_HTTP.getString(typedDictionaryHandle, "replytype", "Error: No reply type received")
    string text = replyType + "\n"
    if(replyType == "conversationResponse")
        int[] npcHandles = F4SE_HTTP.getNestedDictionariesArray(typedDictionaryHandle, "npcs")        
        int iElement = npcHandles.Length
        int iIndex = 0
        text += "NPC handles: "
        While (iIndex < iElement)
            text += npcHandles[iIndex] + ", "
            iIndex += 1
        EndWhile

        text += "NPCs:" + "\n"
        iElement = npcHandles.Length
        iIndex = 0
        While (iIndex < iElement)
            string npcText = GetNPC(npcHandles[iIndex])
            text += npcText + "\n"
            iIndex += 1
        EndWhile

        float testFloat = F4SE_HTTP.getFloat(typedDictionaryHandle, "testfloat", 1)
        text += "testFloat: " + testFloat + "\n"

        string[] testStringArray = F4SE_HTTP.getStringArray(typedDictionaryHandle, "teststringarray")
        iElement = testStringArray.Length
        iIndex = 0
        While (iIndex < iElement)
            text += testStringArray[iIndex] + ", "
            iIndex += 1
        EndWhile

        int[] testIntArray = F4SE_HTTP.getIntArray(typedDictionaryHandle, "testintarray")
        iElement = testIntArray.Length
        iIndex = 0
        While (iIndex < iElement)
            text += testIntArray[iIndex] + ", "
            iIndex += 1
        EndWhile

        float[] testFloatArray = F4SE_HTTP.getFloatArray(typedDictionaryHandle, "testfloatarray")
        iElement = testFloatArray.Length
        iIndex = 0
        While (iIndex < iElement)
            text += testFloatArray[iIndex] + ", "
            iIndex += 1
        EndWhile

        bool[] testBoolArray = F4SE_HTTP.getBoolArray(typedDictionaryHandle, "testboolarray")
        iElement = testBoolArray.Length
        iIndex = 0
        While (iIndex < iElement)
            text += testBoolArray[iIndex] + ", "
            iIndex += 1
        EndWhile


        int contextHandle = F4SE_HTTP.getNestedDictionary(typedDictionaryHandle, "context", 0)
        string current_location = F4SE_HTTP.getString(contextHandle, "location", "Only the gods know where")
        text += "location: " + current_location + "\n"
        int time = F4SE_HTTP.getInt(contextHandle, "time", 0)
        text += "time: " + time + "\n"     
        
    endIf
    
    Debug.MessageBox(text)
endFunction

string function GetNPC(int handle)    
    string result = F4SE_HTTP.getString(handle, "name", "Error: Could not get name of NPC") + "\n"
    result += F4SE_HTTP.getString(handle, "gender", "Error: Could not get gender of NPC") + "\n"
    result += F4SE_HTTP.getBool(handle, "npcLikesPlayer", "Error: Could not get npcLikesPlayer of NPC") + "\n"
    return result
endFunction

event OnInit()
    int H_KEY = 35
    Debug.MessageBox("OnInit for TestPapyrusScript triggered")    
    RegisterForExternalEvent("OnHttpReplyReceived","OnHttpReplyReceived")
    RegisterForExternalEvent("OnHttpErrorReceived","OnHttpErrorReceived")
    RegisterForKey(H_KEY)
endEvent

event OnKeyDown(int keyCode)
    int H_KEY = 35
    if keyCode == H_KEY
        Debug.Notification("OnKeyDown for TestPapyrusScript triggered")
        DoSomething()
    endIf
endEvent

function DoSomething()
    int handle = F4SE_HTTP.createDictionary()
    F4SE_HTTP.setString(handle, "requestType", "startConversation")
    ;Debug.MessageBox(F4SE_HTTP.getString(handle,"requestType", "Did not work"))
    int[] npcs = new int[2]
    npcs[0] = SetNPC("Lydia", "female", false)
    npcs[1] = SetNPC("Bandit", "male", true)
    F4SE_HTTP.setNestedDictionariesArray(handle, "npcs", npcs)
    
    F4SE_HTTP.setFloat(handle, "testFloat", 0.123)
    string[] testStringArray = new string[2]
    testStringArray[0] = "trala"
    testStringArray[1] = "lala"
    ;Debug.MessageBox(testStringArray[0] + "\n" + testStringArray[1])
    F4SE_HTTP.setStringArray(handle, "testStringArray", testStringArray)
    int[] testIntArray = new int[2]
    testIntArray[0] = 22
    testIntArray[1] = 23
    F4SE_HTTP.setIntArray(handle, "testIntArray", testIntArray)
    float[] testFloatArray = new float[2]
    testFloatArray[0] = 0.123
    testFloatArray[1] = 0.456
    F4SE_HTTP.setFloatArray(handle, "testFloatArray", testFloatArray)
    bool[] testBoolArray = new bool[2]
    testBoolArray[0] = false
    testBoolArray[1] = true
    F4SE_HTTP.setBoolArray(handle, "testBoolArray", testBoolArray)

    int handleForContext = F4SE_HTTP.createDictionary()
    F4SE_HTTP.setString(handleForContext, "location", "Dragonsreach")
    F4SE_HTTP.setInt(handleForContext, "time", 1328)
    F4SE_HTTP.setNestedDictionary(handle,"context",handleForContext)

    F4SE_HTTP.sendLocalhostHttpRequest(handle,5000,"mantella")    
endFunction

int function SetNPC(string name, string gender, bool isInCombatWithPlayer)
    int npcHandle = F4SE_HTTP.createDictionary()
    F4SE_HTTP.setString(npcHandle, "name", name)
    F4SE_HTTP.setString(npcHandle,"gender", gender)
    F4SE_HTTP.setBool(npcHandle, "isInCombatWithPlayer", isInCombatWithPlayer)
    return npcHandle
endFunction
