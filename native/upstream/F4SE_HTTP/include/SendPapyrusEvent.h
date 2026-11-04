template <class... Args>
static void SendPapyrusEvent(std::string a_eventName, Args... _args) {
    struct PapyrusEventData {
        RE::BSScript::IVirtualMachine* vm;
        RE::BSTThreadScrapFunction<bool(RE::BSScrapArray<RE::BSScript::Variable>&)> scrapFunc;
    };

    PapyrusEventData evntData;
    auto const papyrus = F4SE::GetPapyrusInterface();
    auto* vm = RE::GameVM::GetSingleton()->GetVM().get();

    evntData.scrapFunc = (Papyrus::FunctionArgs{vm, _args...}).get();
    evntData.vm = vm;

    papyrus->GetExternalEventRegistrations(
        a_eventName, &evntData, [](uint64_t handle, const char* scriptName, const char* callbackName, void* dataPtr) {
            PapyrusEventData* d = static_cast<PapyrusEventData*>(dataPtr);
            d->vm->DispatchMethodCall(handle, scriptName, callbackName, d->scrapFunc, nullptr);
        });
}